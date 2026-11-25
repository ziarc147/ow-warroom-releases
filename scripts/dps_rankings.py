"""포에버 딜량 순위 수집 (GitHub 자동 작업이 하루 한 번 실행 — 개발자 PC에서도 시험 가능).

  python tools/dps_rankings.py [출력 폴더=dist/dps] [--page 저장해 둔 HTML] [--force]

자료: ForeverDB 딜량 순위 https://foreverdb.net/dps-rankings (API·내보내기 없음 → 서버가 그린 HTML 을 읽음,
DECISIONS 2026-11-25). 앱은 ForeverDB 에 직접 접속하지 않고, 여기서 만든 dps.json 만 공개 저장소 릴리스에서 받는다.
표준 라이브러리만 씀.

읽는 것 (하나라도 모양이 다르면 ParseError → 아무것도 쓰지 않고 실패로 끝냄. 예전 파일은 그대로):
  1) 「What the other sims and creators say」 표: #, 전문화(/sim/<직업>/<전문화> 링크), 평균 순위, 자체 시뮬 순위
  2) Sources 목록: 이름(링크), (날짜): 전투 조건
  3) 「Our simulator」 표: #, 전문화, DPS, 중간 50% 범위(Middle half of runs)
  + 클라이언트 빌드 번호(머리말), 자체 시뮬 조건(FAQ 문장: 보스 레벨·전투 시간·레이드 버프·장비)

출력 (<출력>/):
  dps-hist.json   날짜별 스냅샷 (다음 실행이 이어 씀, 앱은 안 받음) {"v":1, "days": {"YYYY-MM-DD": 스냅샷}} 최근 120일
  dps.json        앱이 받는 것: 오늘 스냅샷 + 전문화마다 어제·지난주 대비 순위 변동

요청: 하루 1회. 오늘(한국 날짜) 스냅샷이 이미 있으면 받지 않고 끝냄 (--force 로 다시).
User-Agent 에 앱 이름과 배포 저장소 주소.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import re
import sys
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

URL = "https://foreverdb.net/dps-rankings"
UA = "OWWarroom/1.0 (+https://github.com/ziarc147/ow-warroom-releases)"
KST = dt.timezone(dt.timedelta(hours=9))
KEEP_DAYS = 120
SPECS = 20                       # 지금 딜러 전문화 수 — 바뀌면 파싱 실패로 보고 사람이 확인


class ParseError(Exception):
    pass


# ---------- HTML 읽기 ----------
class _Doc(HTMLParser):
    """제목(h1~h3)·표·목록을 순서대로 모은다. 표 칸은 (글, 링크)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list = []          # ("h", 글) / ("table", [[(글, 링크)…]…]) / ("ul", [(글, 링크)…]) / ("p", 글)
        self.cur = None                 # 지금 쌓는 것
        self.cell = None
        self.href = None
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in ("h1", "h2", "h3"):
            self.blocks.append(["h", ""])
        elif tag == "p":
            self.blocks.append(["p", ""])
        elif tag == "table":
            self.cur = ["table", []]
            self.blocks.append(self.cur)
        elif tag == "tr" and self.cur and self.cur[0] == "table":
            self.cur[1].append([])
        elif tag in ("td", "th") and self.cur and self.cur[0] == "table":
            self.cell = ["", None]
        elif tag == "ul":
            self.cur = ["ul", []]
            self.blocks.append(self.cur)
        elif tag == "li" and self.cur and self.cur[0] == "ul":
            self.cell = ["", None]
        elif tag == "a":
            self.href = a.get("href")
            if self.cell is not None and self.cell[1] is None:
                self.cell[1] = self.href

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif tag in ("td", "th") and self.cell is not None and self.cur and self.cur[0] == "table" and self.cur[1]:
            self.cur[1][-1].append((self.cell[0].strip(), self.cell[1]))
            self.cell = None
        elif tag == "li" and self.cell is not None and self.cur and self.cur[0] == "ul":
            self.cur[1].append((self.cell[0].strip(), self.cell[1]))
            self.cell = None
        elif tag in ("table", "ul"):
            self.cur = None
        elif tag == "a":
            self.href = None

    def handle_data(self, data):
        if self.skip:
            return
        if self.cell is not None:
            self.cell[0] += data
        elif self.blocks and self.blocks[-1][0] in ("h", "p"):
            self.blocks[-1][1] += data


def _num(s: str) -> float:
    try:
        return float(s.replace(",", ""))
    except ValueError:
        raise ParseError(f"숫자가 아님: {s!r}") from None


def _key(href: str | None) -> str:
    m = re.fullmatch(r"/sim/([a-z]+)/([a-z-]+)", href or "")
    if not m:
        raise ParseError(f"전문화 링크 모양이 다름: {href!r}")
    return f"{m.group(1)}/{m.group(2)}"


def _after(blocks, title: str, kind: str):
    """제목 뒤 처음 나오는 kind 블록."""
    for i, b in enumerate(blocks):
        if b[0] == "h" and b[1].strip().lower() == title.lower():
            for nb in blocks[i + 1:]:
                if nb[0] == kind:
                    return nb[1]
                if nb[0] == "h":
                    break
    raise ParseError(f"「{title}」 다음에 {kind} 가 없음")


def _header(rows, want: list[str]):
    got = [c[0].lower() for c in rows[0]] if rows else []
    if got != [w.lower() for w in want]:
        raise ParseError(f"표 머리가 다름: {got} (기대 {want})")
    return rows[1:]


def _ranks(vals: list[int]):
    if sorted(vals) != list(range(1, len(vals) + 1)):
        # 같은 순위(공동)는 허용: 1부터 시작하고 줄어들지 않으면 됨
        if not vals or vals[0] != 1 or any(b < a for a, b in zip(vals, vals[1:])) or max(vals) > len(vals):
            raise ParseError(f"순위 번호가 이상함: {vals}")


GEAR_KO = {"pre-raid": "레이드 전 장비", "bis": "BiS 장비", "best in slot": "BiS 장비", "raid": "레이드 장비"}


def parse(page: str) -> dict:
    d = _Doc()
    d.feed(page)
    B = d.blocks
    text = " ".join(b[1] for b in B if b[0] in ("p", "h"))

    m = re.search(r"client build (\d+(?:\.\d+){3})", text)
    if not m:
        raise ParseError("클라이언트 빌드 번호를 못 찾음")
    build = m.group(1)

    # 1) 평균 순위
    rows = _header(_after(B, "What the other sims and creators say", "table"), ["#", "Spec", "Average place", "Our simulator"])
    avg = []
    for r in rows:
        if len(r) != 4:
            raise ParseError(f"평균 순위 표 칸 수가 다름: {r}")
        avg.append({"k": _key(r[1][1]), "en": r[1][0], "ar": int(_num(r[0][0])), "avg": _num(r[2][0]), "sr": int(_num(r[3][0]))})

    # 2) 출처
    sources = []
    for t, href in _after(B, "Sources", "ul"):
        m = re.fullmatch(r"(.+?) \((\d{4}-\d{2}-\d{2})\):\s*(.+)", " ".join(t.split()), re.S)
        if not m:
            raise ParseError(f"출처 줄 모양이 다름: {t!r}")
        sources.append({"n": m.group(1), "date": m.group(2), "cond": m.group(3), "url": href})
    if not sources:
        raise ParseError("출처가 없음")

    # 3) 자체 시뮬
    rows = _header(_after(B, "Our simulator", "table"), ["#", "Spec", "DPS", "Middle half of runs"])
    sim = []
    for r in rows:
        if len(r) != 4:
            raise ParseError(f"시뮬 표 칸 수가 다름: {r}")
        m = re.fullmatch(r"([\d.,]+)\s*[–-]\s*([\d.,]+)", r[3][0])
        if not m:
            raise ParseError(f"중간 50% 범위 모양이 다름: {r[3][0]!r}")
        lo, hi = _num(m.group(1)), _num(m.group(2))
        dps = _num(r[2][0])
        if not lo <= dps <= hi:
            raise ParseError(f"DPS 가 범위 밖: {dps} {lo}–{hi}")
        sim.append({"k": _key(r[1][1]), "sr": int(_num(r[0][0])), "dps": dps, "lo": lo, "hi": hi})

    if len(avg) != SPECS or len(sim) != SPECS:
        raise ParseError(f"전문화 수가 다름: 평균 {len(avg)}, 시뮬 {len(sim)} (기대 {SPECS})")
    if {a["k"] for a in avg} != {s["k"] for s in sim} or len({a["k"] for a in avg}) != SPECS:
        raise ParseError("두 표의 전문화가 서로 다름")
    _ranks([a["ar"] for a in avg])
    _ranks([s["sr"] for s in sim])
    sr = {s["k"]: s["sr"] for s in sim}
    for a in avg:
        if a["sr"] != sr[a["k"]]:
            raise ParseError(f"{a['k']} 시뮬 순위가 두 표에서 다름 ({a['sr']} / {sr[a['k']]})")

    # 시뮬 조건 (FAQ 문장)
    faq = " ".join(text.split())
    m = re.search(r"level (\d+) boss for (\d+) minutes? with ([^.]*?), in ([a-z -]+?) gear", faq, re.I)
    if not m:
        raise ParseError("자체 시뮬 조건 문장을 못 찾음")
    gear = m.group(4).strip().lower()
    cond = {"boss": int(m.group(1)), "min": int(m.group(2)), "buffs": "raid buff" in m.group(3).lower(),
            "gear": GEAR_KO.get(gear, gear + " gear"), "en": m.group(0)}

    by = {s["k"]: s for s in sim}
    specs = [{**a, "dps": by[a["k"]]["dps"], "lo": by[a["k"]]["lo"], "hi": by[a["k"]]["hi"]} for a in avg]
    return {"build": build, "cond": cond, "sources": sources, "specs": specs}


# ---------- 스냅샷·변동 ----------
def _near(days: dict, target: dt.date, slack: int = 2) -> str | None:
    """target 날 스냅샷, 없으면 그 앞 slack 일 안의 가장 가까운 것."""
    for k in range(slack + 1):
        d = (target - dt.timedelta(days=k)).isoformat()
        if d in days:
            return d
    return None


def changes(today: str, days: dict) -> dict:
    """{"d": 비교한 날(어제), "w": 비교한 날(지난주)} 과 전문화별 {"ar": [어제, 지난주], "sr": [어제, 지난주]}.
    값 = 예전 순위 − 지금 순위 (양수 = 올라감), 비교할 자료가 없으면 None."""
    t = dt.date.fromisoformat(today)
    cur = days[today]
    ref = {"d": _near(days, t - dt.timedelta(days=1)), "w": _near(days, t - dt.timedelta(days=7))}
    out = {}
    for s in cur["specs"]:
        row = {}
        for f in ("ar", "sr"):
            vals = []
            for r in ("d", "w"):
                old = next((x for x in days[ref[r]]["specs"] if x["k"] == s["k"]), None) if ref[r] else None
                vals.append(old[f] - s[f] if old else None)
            row[f] = vals
        out[s["k"]] = row
    return {"ref": ref, "by": out}


def build_app(today: str, days: dict, made: str) -> dict:
    snap = days[today]
    ch = changes(today, days)
    return {"v": 1, "made": made, "day": today, "src": URL, "build": snap["build"], "cond": snap["cond"],
            "sources": snap["sources"], "ref": ch["ref"],
            "specs": [{**s, "ch": ch["by"][s["k"]]} for s in snap["specs"]]}


def fetch(url: str = URL) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8")


def run(out: Path, page: str | None = None, force: bool = False, now: dt.datetime | None = None) -> str:
    """'skip' (오늘 것 있음) / 'ok'. 실패면 예외 — 이때 파일은 하나도 바뀌지 않는다."""
    now = now or dt.datetime.now(KST)
    today = now.date().isoformat()
    out.mkdir(parents=True, exist_ok=True)
    hp = out / "dps-hist.json"
    hist = json.loads(hp.read_text(encoding="utf-8")) if hp.is_file() else {"v": 1, "days": {}}
    days = hist["days"]
    if today in days and not force and page is None:
        print(f"오늘({today}) 자료가 이미 있어 받지 않음")
        return "skip"
    snap = parse(page if page is not None else fetch())
    snap["at"] = now.isoformat(timespec="minutes")
    days[today] = snap
    cut = (now.date() - dt.timedelta(days=KEEP_DAYS)).isoformat()
    hist["days"] = {k: days[k] for k in sorted(days) if k >= cut}
    app = build_app(today, hist["days"], snap["at"])
    # 둘 다 만들어 본 뒤에 한꺼번에 바꿔 끼움
    for name, js in (("dps-hist.json", hist), ("dps.json", app)):
        tmp = out / (name + ".tmp")
        tmp.write_text(json.dumps(js, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    for name in ("dps-hist.json", "dps.json"):
        (out / (name + ".tmp")).replace(out / name)
    print(f"{today}: 전문화 {len(snap['specs'])}개, 출처 {len(snap['sources'])}곳, 빌드 {snap['build']}, "
          f"비교 어제={app['ref']['d']} 지난주={app['ref']['w']}")
    return "ok"


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    page = None
    if "--page" in argv:
        page = Path(argv[argv.index("--page") + 1]).read_text(encoding="utf-8")
        args = [a for a in args if a != argv[argv.index("--page") + 1]]
    out = Path(args[0]) if args else Path(__file__).resolve().parents[1] / "dist" / "dps"
    try:
        run(out, page, force="--force" in argv)
    except ParseError as e:
        print(f"::error::ForeverDB 페이지 모양이 바뀌어 읽지 못했습니다: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main(sys.argv[1:]))
