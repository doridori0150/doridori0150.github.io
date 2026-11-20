"""공개 GitHub Pages 사이트 목록(sites.json)과 썸네일(thumbs/*.webp)을 갱신한다.

- 대상: OWNER 계정의 공개 리포 중 GitHub Pages 가 켜진 것 (이 리포 자신, config.json 의 exclude 제외)
- 썸네일: 없거나 THUMB_MAX_AGE_DAYS 보다 오래됐거나 REFRESH=1 이면 headless Chrome 으로 다시 캡처
- 사용: python scripts/update.py           갱신
        python scripts/update.py --check   캡처가 필요한지만 출력 (need_capture=true/false)
- 환경 변수: GH_TOKEN (선택, API 한도 완화), CHROME (Chrome 실행 파일 경로), REFRESH=1 (전부 재캡처)
"""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

OWNER = "doridori0150"
ROOT = Path(__file__).resolve().parent.parent
THUMBS = ROOT / "thumbs"
SITES_JSON = ROOT / "sites.json"
CONFIG_JSON = ROOT / "config.json"
THUMB_MAX_AGE_DAYS = 7
THUMB_SIZE = (640, 400)
SHOT_SIZE = (1280, 800)


def log(msg: str) -> None:
    print(msg, flush=True)


def http_get(url: str, accept: str = "application/vnd.github+json") -> bytes:
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "pages-index-updater"})
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as res:
        return res.read()


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def list_pages_repos(exclude: set[str]) -> list[dict]:
    repos, page = [], 1
    while True:
        batch = json.loads(http_get(
            f"https://api.github.com/users/{OWNER}/repos?type=owner&per_page=100&page={page}"))
        repos += batch
        if len(batch) < 100:
            break
        page += 1
    self_name = f"{OWNER}.github.io".lower()
    return [
        r for r in repos
        if r.get("has_pages") and not r.get("private") and not r.get("archived") and not r.get("fork")
        and r["name"].lower() != self_name and r["name"] not in exclude
    ]


def page_title(url: str) -> str | None:
    try:
        body = http_get(url, accept="text/html").decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001 - 제목은 없어도 된다
        log(f"  제목 가져오기 실패: {e}")
        return None
    m = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
    return html.unescape(m.group(1)).strip() if m and m.group(1).strip() else None


def find_chrome() -> str:
    cands = [os.environ.get("CHROME"), shutil.which("google-chrome"), shutil.which("chromium"),
             shutil.which("chromium-browser"), shutil.which("chrome"),
             r"C:\Program Files\Google\Chrome\Application\chrome.exe",
             r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"]
    for c in cands:
        if c and Path(c).exists():
            return c
    sys.exit("Chrome 을 찾을 수 없습니다. CHROME 환경 변수로 경로를 지정하세요.")


def is_blank(img) -> bool:
    from PIL import ImageStat
    return max(ImageStat.Stat(img.convert("L")).stddev) < 4


def capture(chrome: str, url: str, out: Path) -> bool:
    from PIL import Image
    for budget in (10000, 25000):
        with tempfile.TemporaryDirectory() as tmp:
            png = Path(tmp) / "shot.png"
            args = [chrome, "--headless=new", "--hide-scrollbars", "--mute-audio", "--no-first-run",
                    "--no-default-browser-check", f"--user-data-dir={tmp}/profile",
                    f"--window-size={SHOT_SIZE[0]},{SHOT_SIZE[1]}", f"--virtual-time-budget={budget}",
                    f"--screenshot={png}", url]
            if sys.platform.startswith("linux"):
                args.insert(1, "--no-sandbox")
            try:
                subprocess.run(args, timeout=90, capture_output=True)
            except subprocess.TimeoutExpired:
                log("  캡처 시간 초과")
                continue
            if not png.exists():
                continue
            img = Image.open(png).convert("RGB")
            if is_blank(img):
                log(f"  빈 화면 (budget={budget}ms), 다시 시도")
                continue
            img.resize(THUMB_SIZE, Image.LANCZOS).save(out, "WEBP", quality=82, method=6)
            return True
    return False


def needs_capture(entry: dict | None, name: str, refresh: bool, now: dt.datetime) -> bool:
    if refresh or not (THUMBS / f"{name}.webp").exists() or not entry or not entry.get("thumb_at"):
        return True
    return now - dt.datetime.fromisoformat(entry["thumb_at"]) > dt.timedelta(days=THUMB_MAX_AGE_DAYS)


def main() -> None:
    check_only = "--check" in sys.argv
    refresh = os.environ.get("REFRESH") in ("1", "true")
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    config = load_json(CONFIG_JSON, {})
    overrides = config.get("overrides", {})
    old = {s["name"]: s for s in load_json(SITES_JSON, {"sites": []}).get("sites", [])}
    repos = list_pages_repos(set(config.get("exclude", [])))

    todo = [r["name"] for r in repos if needs_capture(old.get(r["name"]), r["name"], refresh, now)]
    if check_only:
        print(f"need_capture={'true' if todo else 'false'}")
        out = os.environ.get("GITHUB_OUTPUT")
        if out:
            with open(out, "a", encoding="utf-8") as f:
                f.write(f"need_capture={'true' if todo else 'false'}\n")
        return

    THUMBS.mkdir(exist_ok=True)
    chrome = find_chrome() if todo else None
    sites = []
    for r in repos:
        name = r["name"]
        url = r.get("homepage") or f"https://{OWNER}.github.io/{name}/"
        prev = old.get(name, {})
        log(f"- {name}")
        thumb_at = prev.get("thumb_at")
        thumb = f"thumbs/{name}.webp" if (THUMBS / f"{name}.webp").exists() else None
        if name in todo:
            if capture(chrome, url, THUMBS / f"{name}.webp"):
                thumb, thumb_at = f"thumbs/{name}.webp", now.isoformat()
                log("  썸네일 갱신")
            else:
                log("  썸네일 실패 (기존 것 유지)" if thumb else "  썸네일 없음")
        o = overrides.get(name, {})
        sites.append({
            "name": name,
            "title": o.get("title") or page_title(url) or prev.get("title") or name,
            "description": o.get("description") or r.get("description") or "",
            "url": url,
            "repo": r["html_url"],
            "language": r.get("language"),
            "pushed_at": r.get("pushed_at"),
            "thumb": thumb,
            "thumb_at": thumb_at,
        })

    # 이제 목록에 없는 사이트의 썸네일은 지운다
    keep = {f"{s['name']}.webp" for s in sites}
    for f in THUMBS.glob("*.webp"):
        if f.name not in keep:
            f.unlink()
            log(f"- 썸네일 삭제: {f.name}")

    sites.sort(key=lambda s: s.get("pushed_at") or "", reverse=True)
    data = {"owner": OWNER, "sites": sites}
    prev_data = load_json(SITES_JSON, {})
    prev_data.pop("updated_at", None)
    if prev_data != data:
        data_out = {**data, "updated_at": now.isoformat()}
        SITES_JSON.write_text(json.dumps(data_out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        log(f"sites.json 갱신: {len(sites)}개")
    else:
        log("변경 없음")


if __name__ == "__main__":
    main()
