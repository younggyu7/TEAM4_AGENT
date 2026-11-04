"""개발한 앱의 화면 스타일 (최종 개발하기에서 고름). 앱 틀의 styles.css는 색을 모두 토큰(CSS 변수)으로 쓰므로,
스타일은 그 토큰만 바꾸는 frontend/src/theme.css 한 파일이다 (LLM 없음). AI가 짠 화면도 같은 부품(kit)을 쓰므로 같이 바뀐다.
- light: 밝은 테마에서 바꿀 토큰, dark: 어두운 테마에서 바꿀 토큰 (없는 토큰은 styles.css 기본값)
- preview: 고르는 화면의 작은 미리보기 색 (배경·메뉴·메뉴 글자·강조·강조 글자·표지)
"""
from __future__ import annotations

DEFAULT = "portal"


def _theme(tid, name, note, side, side_ink, accent, soft, line, mark, cover, cover_hi, bg="#f6f7f4", panel2="#eef2ec", rule="#d9e0d7",
           dark_accent="#7fcfa8", dark_side="#0b1412", side_cap=None, side_on=None, side_hover=None, side_line=None, mark_ink=None, cover_ink=None,
           accent_ink="#ffffff", dark_accent_ink="#0d1a24"):
    light = {"--bg": bg, "--panel-2": panel2, "--line": rule, "--accent": accent, "--accent-ink": accent_ink, "--accent-soft": soft,
             "--accent-line": line, "--side": side, "--side-ink": side_ink, "--side-cap": side_cap or side_ink,
             "--side-on": side_on or accent, "--side-hover": side_hover or side_on or accent, "--side-line": side_line or line,
             "--mark": mark, "--mark-ink": mark_ink or side, "--cover": cover, "--cover-ink": cover_ink or side_ink, "--cover-hi": cover_hi,
             "--cover-line": line}
    dark = {"--accent": dark_accent, "--accent-ink": dark_accent_ink, "--accent-line": dark_accent, "--side": dark_side, "--cover": dark_side}
    return {"id": tid, "name": name, "note": note, "light": light, "dark": dark,
            "preview": {"bg": bg, "side": side, "side_ink": side_ink, "accent": accent, "accent_ink": accent_ink, "cover": cover, "mark": mark}}


THEMES = [
    {"id": "portal", "name": "그린 포털 (기본)", "note": "차분한 녹색 메뉴, 업무 포털 기본",
     "light": {}, "dark": {},
     "preview": {"bg": "#f6f7f4", "side": "#17473f", "side_ink": "#d3e5dc", "accent": "#206348", "accent_ink": "#ffffff", "cover": "#164b43", "mark": "#b8ecd5"}},
    _theme("navy", "공공 네이비", "공공기관 업무 시스템 느낌의 남색", "#1b2a4a", "#d6deef", "#1f4fa3", "#e3ebf8", "#8eaee0", "#c9dafa", "#18284a",
           "#a9c8ff", bg="#f4f6fa", panel2="#e9eef6", rule="#d5dce8", dark_accent="#8fb4f0", dark_side="#0c1424",
           side_on="#2e4370", side_hover="#263a62", side_line="#3d5482", cover_ink="#d6deef"),
    _theme("blue", "코퍼레이트 블루", "밝고 선명한 파랑, 기업 서비스", "#0f3d73", "#d8e7fa", "#1769d1", "#e2eefc", "#7fb0ec", "#cfe3ff", "#0f3d73",
           "#9ed0ff", bg="#f5f8fc", panel2="#eaf1fa", rule="#d3deec", dark_accent="#86b8f5", dark_side="#08182c",
           side_on="#1e5596", side_hover="#174a86", side_line="#33639c"),
    _theme("slate", "슬레이트", "무채색 회색, 군더더기 없는 관리 화면", "#2b2f36", "#dfe2e7", "#3f4a5a", "#e7eaee", "#a2abb8", "#d9dde3", "#2b2f36",
           "#cfd6df", bg="#f5f6f7", panel2="#eceef1", rule="#d8dce1", dark_accent="#b7c2d1", dark_side="#121418",
           side_on="#454b55", side_hover="#3a4049", side_line="#535a65"),
    _theme("wine", "와인", "짙은 자주, 격식 있는 문서 업무", "#4a1f2b", "#f0dbe1", "#8c2f45", "#f7e4e9", "#d496a6", "#f4cbd6", "#4a1f2b",
           "#ffc2d1", bg="#faf6f7", panel2="#f3eaec", rule="#e6d6da", dark_accent="#f0a3b6", dark_side="#1c0d12",
           side_on="#6b3240", side_hover="#5d2a37", side_line="#7a4553"),
    _theme("amber", "앰버", "따뜻한 주황, 현장·물류 업무", "#4a3214", "#f3e3cc", "#b5650d", "#fcecd6", "#e5b06c", "#ffd9a8", "#4a3214",
           "#ffd08a", bg="#fbf8f3", panel2="#f4ede2", rule="#e7dccb", dark_accent="#f2b56b", dark_side="#1c130a",
           side_on="#6b4a20", side_hover="#5c3f1b", side_line="#7d5a2c", accent_ink="#ffffff", dark_accent_ink="#2a1a06"),
    _theme("violet", "바이올렛", "보라 강조, 신규 서비스 느낌", "#2e2457", "#e1dcf6", "#5b3fc4", "#ebe6fb", "#a896ea", "#d8cdfc", "#2e2457",
           "#c8b9ff", bg="#f7f6fb", panel2="#eeebf8", rule="#dcd7ec", dark_accent="#b3a0ff", dark_side="#120e24",
           side_on="#43377a", side_hover="#3a2f6b", side_line="#544a8a"),
    _theme("teal", "오션 틸", "청록, 의료·환경 업무", "#0d3b4a", "#d2ecf0", "#0e7c86", "#dcf2f3", "#7cc6cc", "#bfeef0", "#0d3b4a",
           "#9fe6e3", bg="#f4f9f9", panel2="#e8f2f3", rule="#d3e4e6", dark_accent="#7fd6d6", dark_side="#071a20",
           side_on="#1c5566", side_hover="#174a59", side_line="#2c6474"),
    _theme("minimal", "화이트 미니멀", "흰 메뉴와 파란 강조, 가볍고 밝게", "#ffffff", "#273142", "#2b59c3", "#e6edfb", "#93acea", "#e3eaff", "#f1f4fb",
           "#2b59c3", bg="#fafbfc", panel2="#f1f3f6", rule="#e1e5eb", dark_accent="#8fb0f5", dark_side="#11151c",
           side_cap="#6b7686", side_on="#e6edfb", side_hover="#f1f4fb", side_line="#e1e5eb", mark_ink="#2b59c3", cover_ink="#273142"),
]
BY_ID = {t["id"]: t for t in THEMES}


def choices() -> list[dict]:
    """화면에서 고를 스타일 (id·이름·설명·미리보기 색)"""
    return [{"id": t["id"], "name": t["name"], "note": t["note"], "preview": t["preview"]} for t in THEMES]


def css(theme_id: str | None) -> str:
    """frontend/src/theme.css 내용. 기본 스타일이면 바꿀 토큰이 없다 (styles.css 그대로)"""
    t = BY_ID.get(theme_id or DEFAULT) or BY_ID[DEFAULT]
    lines = [f"/* 화면 스타일: {t['name']} — 최종 개발하기에서 고름 (코드가 만든 파일). styles.css의 색 토큰만 바꾼다. */"]
    if t["light"]:
        lines.append(":root {\n" + "".join(f"  {k}: {v};\n" for k, v in t["light"].items()) + "}")
    if t["dark"]:
        block = "".join(f"  {k}: {v};\n" for k, v in t["dark"].items())
        lines.append("@media (prefers-color-scheme: dark) {\n  :root:not([data-theme=\"light\"]) {\n"
                     + "".join(f"  {x}\n" for x in block.rstrip().split("\n")) + "  }\n}")
        lines.append(":root[data-theme=\"dark\"] {\n" + block + "}")
    return "\n".join(lines) + "\n"
