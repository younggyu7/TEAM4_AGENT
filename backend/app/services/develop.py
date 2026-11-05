"""최종 개발하기 (화면): 팀장이 승인한 최종본의 개발 스펙으로 서비스 코드를 만들고 미리보기 화면을 빌드한다.

흐름 (작업 하나, source=develop):
  1) `python -m app develop <프로젝트> --from v<최종본> [--logic]` → 코드 OUTPUT_DIR/<프로젝트>/app (agents/tools/scaffold.py)
     기본은 LLM 없음. --logic이면 업무 로직 빈칸만 LLM이 채운다 (예상 비용은 plan()으로 먼저 보여 준다)
  2) 화면을 예시 데이터 모드로 빌드 → app/.preview/mock (npm install은 package.json이 바뀔 때만)
     우리 백엔드가 /api/v1/preview/<프로젝트>/mock/ 으로 보여 준다 (로그인한 회원만, api/v1/develop.py)
  3) 코드 저장소(services/repo.py)의 main에 req-agent 커밋 + 태그 final-v<최종본> (바뀐 것이 없으면 커밋 없음)
  4) 최종본 버전 기록의 develop에 누가·언제·AI 사용 여부·결과·커밋을 남긴다. 실행 중인 앱(services/preview.py)은 다음에 열 때 다시 빌드한다
코드 폴더는 프로젝트마다 하나다. 다시 개발하면 바뀐 파일만 바뀌고 사람이 고친 파일은 덮어쓰지 않는다 (scaffold.write_app).
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
from urllib.parse import quote
import subprocess
from datetime import datetime
from pathlib import Path

from app.agents.tools.versions import find_version, update_version
from app.core.exceptions import BadRequest, NotFound
from app.services.jobs.common import Job, _new_job, _path_arg, _Runner

PREVIEW = ".preview"      # 코드 폴더 안의 미리보기 빌드·실행 파일 (git에 올리지 않는다)


def app_dir(project: str) -> Path:
    from app.agents.tools import scaffold
    return scaffold.app_dir(project)


def approved_final(project: str, version: str) -> dict:
    """팀장이 승인한 최종본 기록 (아니면 400). 개발은 승인한 최종본에서만 한다"""
    record = find_version(project, version)
    if record is None:
        raise NotFound(f"v{version}이 없습니다.")
    if record.get("stage") != "final":
        raise BadRequest("최종 개발하기는 'ERD 최종 확정'으로 만든 최종본에서만 합니다.")
    if (record.get("approval") or {}).get("decision") != "approve":
        raise BadRequest("팀장이 최종 승인한 최종본만 개발할 수 있습니다.")
    return record


def plan(project: str, version: str) -> dict:
    """개발 전에 보여 줄 것: 화면·파일 수, 백엔드를 만드는지, --logic 예상 토큰·비용 (LLM 없음)"""
    from app.agents.roles import developer
    from app.agents.tools import scaffold

    try:
        spec = scaffold.load_spec(project, version)
    except (scaffold.AppError, ValueError) as e:
        raise BadRequest(f"개발을 시작할 수 없습니다: {e}") from e
    from app.agents.roles import builder
    from app.agents.tools import stacks

    files, data = scaffold.render_app(spec, {})
    todo = developer.plan(spec, app_dir(project), None)
    record = find_version(project, version) or {}
    parts = stacks.parts_of(spec.stack.get("backend", ""), (record.get("tech_stack") or {}).get("parts"))
    missing = stacks.untemplated(parts)
    blocked = stacks.no_develop(parts)
    return {"blocked": stacks.warning(blocked) if blocked else "","version": version, "stack": spec.stack.get("backend", ""), "backend": scaffold.has_backend(spec),
            "parts": parts, "untemplated": missing, "warning": stacks.warning(missing),
            "build": _build_plan(spec, parts, missing, project),
            "menus": sum(len(m["pages"]) for m in data["menu"]), "screens": len(data["screens"]), "autos": len(data["autos"]),
            "files": len(files), "logic": {"functions": todo["functions"], "cached": todo["cached"], "stale": len(todo["stale"]),
                                           "input_tokens": todo["input_tokens"], "output_tokens": todo["output_tokens"],
                                           "cost": todo["cost"]}}


def _build_plan(spec, parts: dict, missing: list[dict], project: str) -> dict:
    """AI 업무 기능 구현 예상 + 틀이 없는 층이면 AI 뼈대 예상(실측 전 가정)을 더한다"""
    from app.agents.roles import builder, scaffolder

    out = builder.plan(spec, app_dir(project), None)
    layers = sorted({"backend" if m["layer"] == "database" else m["layer"] for m in missing})
    if layers:
        extra = scaffolder.plan(spec, parts, layers, app_dir(project))
        out = {**out, "scratch": extra, "input_tokens": out["input_tokens"] + extra["input_tokens"],
               "output_tokens": {e: v + int(extra["output_tokens"] * builder.EFFORT_FACTOR[e]) for e, v in out["output_tokens"].items()}}
    return out


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""


class _DevelopRunner(_Runner):
    """develop CLI → 화면 빌드 → 버전 기록. 새 설계 버전을 만들지 않으므로 _Runner의 '새 버전 찾기'를 쓰지 않는다"""

    def __init__(self, job: Job, project: str, version: str, build: bool, ai: dict, member):
        super().__init__(job, project, Path("."))
        self.version, self.build, self.ai, self.member = version, build, ai, member

    def _sh(self, cmd: list[str], cwd: Path, env: dict | None = None) -> int:
        self._push(f"$ {' '.join(cmd)}")
        try:
            proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace", env={**os.environ, **(env or {})})
        except OSError as e:
            self._push(f"{cmd[0]}을(를) 실행할 수 없습니다: {e}")
            return -1
        for line in proc.stdout:
            self._push(line)
        return proc.wait()

    def _run(self, args: list[str]) -> None:
        try:
            mode = self.ai.get("mode")
            self.job.message = "개발 스펙으로 코드를 만드는 중" + (" (업무 기능은 AI가 구현 — 몇 분 걸립니다)" if mode == "build"
                                                          else " (빈 업무 로직은 AI가 채움)" if mode == "logic" else " (LLM 없음)")
            self._save()
            if self._exec(args) != 0:
                return self._finish("failed", "코드를 만들지 못했습니다. 아래 기록의 마지막 줄을 확인하세요.")
            built = self.build_mock()
            summary = self.summary()
            commit = self.commit()
            check = self.check()
            record = find_version(self.project, self.version) or {}
            develop = {"version": self.version, "at": datetime.now().isoformat(timespec="seconds"),
                       "by": {"id": self.member.id, "name": self.member.name, "role": self.member.role},
                       "build": self.build, "ai": self.ai, "logic": self.ai.get("mode") == "logic", "mock": built,
                       "backend": (app_dir(self.project) / "backend").is_dir(), "summary": summary, "check": check,
                       "usage": self.usage(), "commit": commit,
                       "log": [line for line in self.job.log if line.lstrip().startswith(("새로 쓴", "사람이 고쳐", "기능 구현(LLM)", "스펙 v"))][-6:]}
            update_version(self.project, self.version, develop=develop, developments=[*(record.get("developments") or []), develop])
            self.job.new_version = self.version
            if check and not check.get("ok"):
                problem = check.get("message") or f"서버 오류 {len(check.get('errors') or [])}건"
                return self._finish("done", f"개발했지만 자동 확인에서 문제가 있습니다: {problem}")
            self._finish("done", "개발했습니다. 최종 개발물 1단계에서 결과를, 2단계에서 실행 중인 앱을 볼 수 있습니다.")
        except Exception as e:  # noqa: BLE001 - 작업 스레드의 오류는 화면에 보여 준다
            self._push(repr(e))
            self._finish("failed", f"작업 중 오류가 났습니다: {e}")

    def commit(self) -> str | None:
        """main에 이번 개발을 커밋한다 (작성자 req-agent). 저장소 오류는 기록만 하고 개발은 끝낸다"""
        from app.services import repo

        try:
            sha = repo.commit_develop(self.project, self.version, repo.develop_message(self.version, self.ai, self.member.name))
        except Exception as e:  # noqa: BLE001 - 커밋 실패로 개발 결과를 버리지 않는다
            self._push(f"코드 저장소에 커밋하지 못했습니다: {e}")
            return None
        self._push(f"코드 저장소 main에 커밋: {sha[:10]} (태그 final-v{self.version})" if sha else "코드 저장소: 바뀐 파일이 없어 커밋하지 않음")
        return sha

    def usage(self) -> dict:
        """이번 개발에 쓴 LLM: CLI 기록의 'LLM n회, 입력 x · 출력 y 토큰' 줄(기능 구현·업무 로직·AI 뼈대)을 더한다"""
        calls = inp = out = 0
        for line in self.job.log:
            if m := re.search(r"LLM (\d+)회, 입력 ([\d,]+) · 출력 ([\d,]+) 토큰", line):
                calls, inp, out = calls + int(m.group(1)), inp + int(m.group(2).replace(",", "")), out + int(m.group(3).replace(",", ""))
        return {"calls": calls, "input": inp, "output": out, "model": self.ai.get("model", ""), "effort": self.ai.get("effort", ""),
                "mode": self.ai.get("mode") or "none"}

    def summary(self) -> dict:
        """기능별 구현 방식: AI 구현 · AI 실패(코드 틀로 둠) · 코드 틀, 그리고 고객 답을 기다려 구현하지 않은 규칙 (LLM 없음)"""
        from app.agents.roles import builder
        from app.agents.tools import scaffold

        spec = scaffold.load_spec(self.project, self.version)
        owned, _ = scaffold.owned_operations(spec)
        records = builder.load_built(app_dir(self.project)) if self.build else {}
        ai = sorted(fid for fid, r in records.items() if r.get("status") == "채움")
        failed = sorted(fid for fid, r in records.items() if r.get("status") == "실패")
        todos = [{"function": fid, **t} for fid in ai for t in records[fid].get("todos") or []]
        functions = [f.function_id for f in spec.functions]
        out = {"functions": len(functions), "with_api": sum(1 for fid in functions if owned.get(fid)), "ai": ai, "failed": failed,
               "template": [fid for fid in functions if fid not in ai], "todos": todos[:50], "todo_count": len(todos)}
        if self.ai.get("mode") == "logic":   # 빈 로직만 AI: 기능마다 채운 업무 로직 자리 수
            logic = {fid: r for fid, r in scaffold.load_logic(app_dir(self.project)).items() if fid in functions}
            filled = {fid: len(r.get("hooks") or {}) for fid, r in logic.items() if r.get("hooks")}
            ltodos = [{"function": fid, **t} for fid, r in logic.items() for t in r.get("todos") or []]
            out.update(logic_functions=sorted(filled), logic_holes=sum(filled.values()), todos=ltodos[:50], todo_count=len(ltodos))
        return out

    def check(self) -> dict | None:
        """자동 확인: 앱을 Docker로 켜고 admin으로 로그인해 모든 조회 API를 부른다. 서버 코드가 없거나 Docker가 없으면 건너뛴다"""
        from app.services import preview

        if not preview.runnable(self.project):
            return None
        if not preview.status(self.project)["docker"]:
            return {"ok": False, "message": "서버 PC에 Docker가 없어 자동 확인을 하지 못했습니다."}
        self.job.message = "자동 확인: 앱을 Docker로 켜는 중 (처음엔 빌드로 몇 분)"
        self._save()
        preview.mark_stale(self.project)   # 새 코드로 다시 빌드
        preview.start(self.project)
        st = preview.wait(self.project)
        if st["status"] != "running":
            return {"ok": False, "message": f"앱을 켜지 못했습니다: {st['message']}"}
        self.job.message = "자동 확인: 로그인하고 조회 API를 부르는 중"
        self._save()
        result = preview.smoke(self.project)
        counts = result.get("counts") or {}
        self._push(f"자동 확인: 조회 API {result.get('apis', 0)}개 · 정상 {counts.get('ok', 0)} · 구현 대기(501) {counts.get('not_implemented', 0)}"
                   f" · 서버 오류 {counts.get('errors', 0)}" + (f" — {result.get('message')}" if result.get("message") else ""))
        return result

    def build_mock(self) -> bool:
        """화면을 예시 데이터 모드로 빌드한다 (서버 없이 모든 화면이 돈다). 상대 경로(./)라 어느 주소 아래서도 열린다"""
        self.job.message = "미리보기 화면을 빌드하는 중"
        self._save()
        return build_frontend(self, self.project, "mock", {})


def build_frontend(runner: _DevelopRunner | object, project: str, mode: str, env: dict) -> bool:
    """app/frontend를 app/.preview/<mode>로 빌드한다. npm install은 package.json이 바뀌었을 때만. runner는 _sh·_push가 있는 것"""
    front = app_dir(project) / "frontend"
    if not (front / "package.json").is_file():
        runner._push("화면 코드(frontend)가 없습니다.")
        return False
    if shutil.which("npm") is None:
        runner._push("서버 PC에 Node.js(npm)가 없어 화면을 빌드할 수 없습니다.")
        return False
    stamp = app_dir(project) / PREVIEW / "npm.hash"
    stamp.parent.mkdir(parents=True, exist_ok=True)
    installed = stamp.read_text(encoding="utf-8") if stamp.is_file() else ""
    if not (front / "node_modules").is_dir() or installed != _hash(front / "package.json"):
        if runner._sh(["npm", "install", "--no-audit", "--no-fund"], front) != 0:
            return False
        stamp.write_text(_hash(front / "package.json"), encoding="utf-8")
    out = app_dir(project) / PREVIEW / mode
    if (front / "next.config.mjs").is_file():   # Next.js 틀: 정적 내보내기(out/)를 미리보기 주소(하위 경로) 기준으로 빌드해 옮긴다
        base = f"/api/v1/preview/{quote(project)}/{mode}"
        if runner._sh(["npx", "next", "build"], front, {**env, "NEXT_BASE_PATH": base}) != 0:
            return False
        shutil.rmtree(out, ignore_errors=True)
        shutil.copytree(front / "out", out)
        return True
    return runner._sh(["npx", "vite", "build", "--base", "./", "--outDir", str(out), "--emptyOutDir"], front, env) == 0


def start_develop(*, project: str, version: str, build: bool, model: str, effort: str, member, logic: bool = False, theme: str = "") -> Job:
    """build: 업무 기능 전체를 AI가 구현 (--build) · logic: 빈 업무 로직 자리만 AI (--logic) · 둘 다 아니면 코드 틀만 (LLM 없음).
    theme: 화면 스타일 (프로젝트에 저장해 다음 개발도 같은 스타일)"""
    job = _new_job(project=project, base=version, source="develop", message=f"최종본 v{version}을 개발하는 중")
    from app.core.config import get_settings
    args = ["develop", project, "--from", f"v{version}", "--output-dir", _path_arg(get_settings().output_dir)]
    ai = {}
    if theme:
        args += ["--theme", theme]
    if build or logic:
        args.append("--build" if build else "--logic")
        if model:
            args += ["--provider", "anthropic" if model.startswith("claude") else "openai", "--model", model]
        if effort:
            args += ["--effort", effort]
        ai = {"mode": "build" if build else "logic", "model": model, "effort": effort}
    _DevelopRunner(job, project, version, build, ai, member).start(args)
    return job
