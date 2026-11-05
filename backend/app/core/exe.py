"""외부 프로그램(npm·npx·mvn·docker) 경로 찾기.

Windows에서는 같은 폴더에 확장자 없는 셸 스크립트(mvn·docker 등)와 실행 파일(.exe·.cmd)이 함께 있어,
shutil.which가 확장자 없는 쪽을 고르면 subprocess가 'WinError 193 올바른 Win32 응용 프로그램이 아닙니다'로 실패한다.
그래서 Windows는 .exe·.cmd·.bat을 먼저 찾는다. npm·npx는 Windows에서 .cmd라 이름만으로는 실행되지 않으므로 이 경로를 쓴다.
"""
from __future__ import annotations

import os
import shutil


def which(name: str) -> str | None:
    if os.name == "nt":
        for ext in (".exe", ".cmd", ".bat"):
            if path := shutil.which(name + ext):
                return path
    return shutil.which(name)


def run_shell(path: str | None) -> bool:
    """이 경로를 subprocess로 돌릴 때 shell이 필요한가 (Windows의 .cmd·.bat)"""
    return bool(path) and os.name == "nt" and path.lower().endswith((".cmd", ".bat"))
