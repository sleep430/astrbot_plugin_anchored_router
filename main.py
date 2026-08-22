from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import register
from astrbot.core import logger
from astrbot.core.agent.message import TextPart
from astrbot.core.agent.tool import FunctionTool, ToolSet

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except Exception:  # pragma: no cover
    get_astrbot_data_path = None

_PLUGIN = "astrbot_plugin_anchored_router"
_STATE_FILE = "state.json"
_SEED_FILE = "seed_round1.json"
_DSH_TOOLS_FILE = "dsh_tools.json"
# Seed tool-call ids carry this prefix so injected rounds are recognized in
# later requests without re-injecting (the history row is the idempotency mark).
_SEED_ID_PREFIX = "call_anchor_seed_"

# dsh Minimal-condition system prompt (verbatim, byte-identical to dsh wire).
_DSH_SYSTEM_LINE = "You are a helpful software engineer assistant."
_PERSONA_MARK = "# Persona Instructions"

# Part 3: appended to the first real user message on the injection round.
# Deliberately does not mention any previous environment.
_TOOL_UPDATE_REMINDER = (
    "<system_reminder>Tooling notice: the set of available tools has been updated. "
    "The tool list provided with the current request is authoritative; "
    "always choose tools from that list.</system_reminder>"
)

_STRATEGY_REMINDER = "reminder"
_STRATEGY_REGISTER = "register"
_STRATEGY_MINIMAL = "minimal"
_STRATEGY_MINIMAL_LEAN = "minimal_lean"
_STRATEGY_MINIMAL_PROMOTE = "minimal_promote"
_STRATEGIES = (
    _STRATEGY_REMINDER,
    _STRATEGY_REGISTER,
    _STRATEGY_MINIMAL,
    _STRATEGY_MINIMAL_LEAN,
    _STRATEGY_MINIMAL_PROMOTE,
)

# minimal_promote 策略的会话面状态（存于 state.json targets[key]["mode"]）。
_MODE_MINIMAL = "minimal"
_MODE_FULL = "full"

# minimal 策略下 tools 字段只保留这两个（dsh 极简面）。
_KEEP_MINIMAL_TOOLS = {"bash", "str_replace_editor"}

# 散文点名其他工具的 system prompt 段落（不清理会诱发模型对被移除工具
# 输出明文 DSML 调用，见 format-research/FORMAT_REPORT.md 附录 2）。
_SKILL_START = "\n## Skills\n\n"
_SKILL_END = "issue clearly and continue with the best alternative."
# Current workspace 段：minimal 面下改写（而非删除）——保留 workspace 路径
# 信息，把原版工具名替换为 bash / str_replace_editor。
_WORKSPACE_SECTION_RE = re.compile(
    r"\n?Current workspace: `([^`\n]*)`\. .*?"
    r"(do not assume this behavior for other tools\.)\n?",
    re.DOTALL,
)
_WS_PATH_RE = re.compile(r"Current workspace: `([^`\n]+)`")
# local env 段中描述 AstrBot 托管 shell session 的句子：dsh bash 是一次性
# 命令执行，无 session 概念，这些句子在 minimal 面下删除。
_SESSION_SENTENCES_RE = re.compile(
    r"Local shell commands automatically return a managed session.*?"
    r"receives a real line feed\. ?",
    re.DOTALL,
)

# 会话 workspace 路径（umo -> path），on_llm_request 时从 system prompt 提取，
# 供 dsh bash/editor handler 作默认工作目录。
_WORKSPACE_BY_UMO: dict[str, str] = {}

# minimal_promote：等待晋升的会话（umo 集合）。on_llm_request 在 minimal 态
# 每轮把会话放入该集合；模型首次调用工具（minimal 面下必为 dsh 两件套之一）
# 时由 handler 在返回文本尾部追加工具更新 reminder 并移出集合——AstrBot 的
# on_llm_tool_respond 钩子在工具消息序列化之后才触发，在那里改 tool_result
# 已不会进入上下文，因此 reminder 必须由 handler 输出携带。
_PROMOTE_PENDING: set[str] = set()


def _promotion_reminder_suffix(event: AstrMessageEvent) -> str:
    """若该会话正等待 minimal_promote 晋升，返回追加在工具结果尾部的
    reminder（一次性）；否则返回空串。"""
    umo = getattr(event, "unified_msg_origin", None)
    if umo and umo in _PROMOTE_PENDING:
        _PROMOTE_PENDING.discard(umo)
        return "\n\n" + _TOOL_UPDATE_REMINDER
    return ""

_BASH_TIMEOUT_S = 120
_BASH_MAX_OUTPUT = 30000


def _data_dir() -> str:
    if get_astrbot_data_path:
        base = get_astrbot_data_path()
    else:  # pragma: no cover
        base = os.path.join(os.getcwd(), "data")
    path = os.path.join(base, "plugin_data", _PLUGIN)
    os.makedirs(path, exist_ok=True)
    return path


def _state_path() -> str:
    return os.path.join(_data_dir(), _STATE_FILE)


def _now() -> int:
    return int(time.time())


def _target_key(umo: str, cid: str | None = None) -> str:
    return f"{umo}::cid::{cid}" if cid else umo


def _load_seed() -> list[dict[str, Any]]:
    """Read the bundled seed round (a recorded dsh Minimal-condition first turn,
    re-cut to AstrBot-native message shapes)."""
    path = os.path.join(os.path.dirname(__file__), _SEED_FILE)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list) or not data:
        raise ValueError(f"seed file {path} is empty or not a message list")
    return data


def _load_dsh_tool_specs() -> list[dict[str, Any]]:
    """Read the verbatim dsh Minimal tool schemas captured from the dsh wire."""
    path = os.path.join(os.path.dirname(__file__), _DSH_TOOLS_FILE)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list) or not data:
        raise ValueError(f"dsh tool spec file {path} is empty or not a list")
    return data


def _has_seed(contexts: list[dict[str, Any]]) -> bool:
    """Whether the request contexts already contain the injected seed round."""
    for msg in contexts:
        if not isinstance(msg, dict):
            continue
        tool_call_id = msg.get("tool_call_id")
        if isinstance(tool_call_id, str) and tool_call_id.startswith(_SEED_ID_PREFIX):
            return True
        for call in msg.get("tool_calls") or []:
            if isinstance(call, dict):
                cid = call.get("id")
                if isinstance(cid, str) and cid.startswith(_SEED_ID_PREFIX):
                    return True
    return False


def _apply_dsh_system_line(req: ProviderRequest) -> None:
    """Part 1: place the dsh Minimal system line at the very front of the system
    prompt and inside the persona section. Idempotent; runs on every bound
    request because AstrBot reassembles the system prompt each round."""
    sp = req.system_prompt or ""
    changed = False
    if not sp.startswith(_DSH_SYSTEM_LINE):
        sp = _DSH_SYSTEM_LINE + ("\n\n" + sp if sp else "")
        changed = True
    mark = _PERSONA_MARK + "\n\n"
    idx = sp.find(mark)
    if idx != -1:
        after = idx + len(mark)
        if not sp[after : after + 100].startswith(_DSH_SYSTEM_LINE):
            sp = sp[:after] + _DSH_SYSTEM_LINE + "\n\n" + sp[after:]
            changed = True
    if changed:
        req.system_prompt = sp


async def _dsh_bash_handler(event: AstrMessageEvent, **kwargs: Any) -> str:
    """Real bash execution backing the migrated dsh `bash` tool."""
    command = kwargs.get("command")
    if not isinstance(command, str) or not command.strip():
        return "error: missing required parameter: command"
    workdir = kwargs.get("workdir") or None
    if isinstance(workdir, str) and not workdir.strip():
        workdir = None
    if workdir is None:
        # dsh parity: the tool's default cwd is the session workspace, not the
        # AstrBot process cwd. Fall back to the process cwd when the workspace
        # directory does not exist (e.g. sessions without Computer Use).
        ws = _WORKSPACE_BY_UMO.get(event.unified_msg_origin)
        if ws and os.path.isdir(ws):
            workdir = ws
    shell = shutil.which("bash") or shutil.which("sh")
    if not shell:
        return "error: no bash/sh shell found on this host"
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            shell,
            "-c",
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=workdir,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_BASH_TIMEOUT_S)
        text = out.decode("utf-8", "replace")
    except TimeoutError:
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
        return f"error: command timed out after {_BASH_TIMEOUT_S}s"
    except Exception as e:
        return f"error: failed to run command: {e}"
    if len(text) > _BASH_MAX_OUTPUT:
        text = text[:_BASH_MAX_OUTPUT] + f"\n... [truncated, {len(text)} chars total]"
    if not text:
        rc = proc.returncode if proc is not None else "?"
        text = f"(no output, exit code {rc})"
    return text


async def _dsh_editor_handler(event: AstrMessageEvent, **kwargs: Any) -> str:
    """Real file view/create/edit backing the migrated dsh `str_replace_editor` tool."""
    command = kwargs.get("command")
    path = kwargs.get("path")
    if not isinstance(path, str) or not path.strip():
        return "error: missing required parameter: path"
    p = Path(path)
    if not p.is_absolute():
        ws = _WORKSPACE_BY_UMO.get(event.unified_msg_origin)
        if ws and os.path.isdir(ws):
            p = Path(ws) / p

    if command == "view":
        if p.is_dir():
            entries: list[str] = []
            try:
                for child in sorted(p.iterdir()):
                    if child.name.startswith("."):
                        continue
                    entries.append(child.name + ("/" if child.is_dir() else ""))
                    if child.is_dir():
                        for grand in sorted(child.iterdir()):
                            if grand.name.startswith("."):
                                continue
                            entries.append(f"  {child.name}/{grand.name}")
            except Exception as e:
                return f"error: cannot list directory: {e}"
            return "\n".join(entries) or "(empty directory)"
        if not p.is_file():
            return f"error: no such file or directory: {path}"
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception as e:
            return f"error: cannot read file: {e}"
        view_range = kwargs.get("view_range")
        start, end = 1, len(lines)
        if isinstance(view_range, list) and len(view_range) == 2:
            start = max(1, int(view_range[0]))
            end = len(lines) if int(view_range[1]) == -1 else min(len(lines), int(view_range[1]))
        numbered = [f"{i:>6}\t{lines[i - 1]}" for i in range(start, end + 1)]
        return "\n".join(numbered) or "(empty file)"

    if command == "create":
        if p.exists():
            return f"error: file already exists: {path}"
        file_text = kwargs.get("file_text")
        if not isinstance(file_text, str):
            return "error: create requires parameter: file_text"
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(file_text, encoding="utf-8")
        except Exception as e:
            return f"error: cannot create file: {e}"
        return f"File created successfully at: {path}"

    if command == "str_replace":
        old_str = kwargs.get("old_str")
        new_str = kwargs.get("new_str")
        if not isinstance(old_str, str) or old_str == "":
            return "error: str_replace requires parameter: old_str"
        if not isinstance(new_str, str):
            return "error: str_replace requires parameter: new_str"
        if not p.is_file():
            return f"error: no such file: {path}"
        content = p.read_text(encoding="utf-8", errors="replace")
        count = content.count(old_str)
        if count == 0:
            return "error: old_str not found in file"
        if count > 1:
            return f"error: old_str is not unique ({count} occurrences)"
        p.write_text(content.replace(old_str, new_str, 1), encoding="utf-8")
        return "Edit applied successfully."

    if command == "insert":
        insert_line = kwargs.get("insert_line")
        new_str = kwargs.get("new_str")
        if not isinstance(insert_line, int):
            return "error: insert requires integer parameter: insert_line"
        if not isinstance(new_str, str):
            return "error: insert requires parameter: new_str"
        if not p.is_file():
            return f"error: no such file: {path}"
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        insert_line = max(0, min(insert_line, len(lines)))
        lines.insert(insert_line, new_str)
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return "Insert applied successfully."

    return f"error: unknown command: {command!r} (expected view/create/str_replace/insert)"


async def _dsh_bash_tool(event: AstrMessageEvent, **kwargs: Any) -> str:
    """bash 工具入口：执行后在 minimal_promote 待晋升会话上追加 reminder。"""
    text = await _dsh_bash_handler(event, **kwargs)
    return text + _promotion_reminder_suffix(event)


async def _dsh_editor_tool(event: AstrMessageEvent, **kwargs: Any) -> str:
    """str_replace_editor 工具入口：同 bash，携带晋升 reminder。"""
    text = await _dsh_editor_handler(event, **kwargs)
    return text + _promotion_reminder_suffix(event)


_DSH_TOOL_HANDLERS = {
    "bash": _dsh_bash_tool,
    "str_replace_editor": _dsh_editor_tool,
}


def _ensure_tool_set(req: ProviderRequest) -> ToolSet:
    """Guarantee req.func_tool exists. AstrBot normally populates it only when
    tool use is enabled; with tools fully disabled it stays None, and the dsh
    tools must still be registered or the request would go out with no tools
    at all (inviting plaintext DSML calls against the tool-naming seed)."""
    if req.func_tool is None:
        req.func_tool = ToolSet()
    return req.func_tool


def _build_dsh_tools() -> list[FunctionTool]:
    tools: list[FunctionTool] = []
    for spec in _load_dsh_tool_specs():
        handler = _DSH_TOOL_HANDLERS.get(spec.get("name"))
        if handler is None:
            continue
        tools.append(
            FunctionTool(
                name=spec["name"],
                description=spec.get("description", ""),
                parameters=spec.get("parameters") or {"type": "object", "properties": {}},
                handler=handler,
            )
        )
    return tools


def _strip_skills_block(sp: str) -> str:
    """Remove the `## Skills` section (it names astrbot_* tools in its rules)."""
    start = sp.find(_SKILL_START)
    if start == -1:
        start = sp.find("## Skills\n\n")
        if start == -1:
            return sp
    end = sp.find(_SKILL_END, start)
    if end == -1:
        m = re.search(r"\n(?=#{1,2}\s)", sp[start + 1 :])
        stop = start + 1 + m.start() if m else len(sp)
        return sp[:start] + sp[stop:]
    stop = end + len(_SKILL_END)
    while stop < len(sp) and sp[stop] == "\n":
        stop += 1
    return sp[:start] + sp[stop:]


def _rewrite_tool_prose(sp: str) -> str:
    """Rewrite system-prompt passages that name the removed AstrBot tools so
    they describe the dsh Minimal pair instead (1.1.2: replace, not delete).

    - `## Skills` block: removed entirely (its rules name astrbot_* tools).
    - Current workspace section: path kept, tool names swapped for
      bash / str_replace_editor.
    - Managed-shell-session sentences in the local-env paragraph: removed
      (the dsh bash tool has no session concept).
    """
    sp = _strip_skills_block(sp)

    def _ws_sub(m: re.Match) -> str:
        path, tail = m.group(1), m.group(2)
        return (
            f"\nCurrent workspace: `{path}`. `bash` uses it as its working "
            f"directory. `str_replace_editor` resolves relative paths from it. "
            f"Prefer relative paths within the workspace; {tail}\n"
        )

    sp = _WORKSPACE_SECTION_RE.sub(_ws_sub, sp)
    sp = _SESSION_SENTENCES_RE.sub("", sp)
    sp = re.sub(r"\n{3,}", "\n\n", sp)
    return sp


def _apply_minimal_strategy(req: ProviderRequest) -> None:
    """Keep only the dsh Minimal tools in the request (registering them first),
    and rewrite system-prompt prose that names any of the removed tools."""
    tool_set = _ensure_tool_set(req)
    for tool in _build_dsh_tools():
        tool_set.add_tool(tool)
    for existing in list(tool_set.tools):
        if existing.name not in _KEEP_MINIMAL_TOOLS:
            tool_set.remove_tool(existing.name)
    req.system_prompt = _rewrite_tool_prose(req.system_prompt or "")


@register(
    _PLUGIN,
    "generated",
    "为绑定会话注入合成的 dsh 极简首轮对话（warm-up），或按 1.1.2 策略维持极简工具面并在首次工具调用后晋升。",
    "1.1.2",
)
class AnchoredRouter(star.Star):
    def __init__(self, context: star.Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.context = context
        self._lock = asyncio.Lock()
        # minimal_promote mid-loop promotion state: full-catalog backup and the
        # live ProviderRequest reference, keyed by binding key.
        self._promote_backup: dict[str, list[Any]] = {}
        self._promote_req: dict[str, Any] = {}
        cfg = config or {}
        strategy = str(cfg.get("tool_strategy", _STRATEGY_REMINDER)).strip()
        if strategy not in _STRATEGIES:
            strategy = _STRATEGY_REMINDER
        self._strategy = strategy

    def _current_strategy(self) -> str:
        """Re-read the plugin config file per request so the strategy can be
        flipped without a plugin reload (test-harness convenience)."""
        try:
            base = (
                get_astrbot_data_path()
                if get_astrbot_data_path
                else os.path.join(os.getcwd(), "data")
            )
            path = os.path.join(base, "config", f"{_PLUGIN}_config.json")
            with open(path, encoding="utf-8-sig") as f:
                cfg = json.load(f)
            strategy = str(cfg.get("tool_strategy", self._strategy)).strip()
            if strategy in _STRATEGIES:
                return strategy
        except Exception:
            pass
        return self._strategy

    async def _load(self) -> dict[str, Any]:
        path = _state_path()
        if not os.path.exists(path):
            return {"version": 1, "targets": {}}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or "targets" not in data:
                return {"version": 1, "targets": {}}
            return data
        except Exception as e:
            logger.error(f"[{_PLUGIN}] load state failed: {e}")
            return {"version": 1, "targets": {}}

    async def _save(self, data: dict[str, Any]) -> None:
        path = _state_path()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    async def _bind(self, umo: str, cid: str | None = None) -> None:
        async with self._lock:
            data = await self._load()
            data["targets"][_target_key(umo, cid)] = {
                "umo": umo,
                "cid": cid,
                "created_at": _now(),
                "updated_at": _now(),
                "last_hit_at": None,
            }
            await self._save(data)

    async def _unbind(self, key: str) -> bool:
        async with self._lock:
            data = await self._load()
            existed = key in data["targets"]
            if existed:
                data["targets"].pop(key, None)
                await self._save(data)
            return existed

    def _match(self, tgt: dict[str, Any], event: AstrMessageEvent, req: ProviderRequest) -> bool:
        if tgt.get("umo") != event.unified_msg_origin:
            return False
        cid = tgt.get("cid")
        if cid:
            req_cid = getattr(req.conversation, "cid", None) if req.conversation else None
            return str(cid) == str(req_cid)
        return True

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("anchor_here")
    async def bind_here(self, event: AstrMessageEvent):
        """绑定当前聊天会话（按 unified_msg_origin）：本会话每个对话的首轮前注入 warm-up 首轮。"""
        await self._bind(event.unified_msg_origin, None)
        yield event.plain_result(
            "已绑定当前会话（按 unified_msg_origin）。\n"
            f"UMO: {event.unified_msg_origin}\n"
            "下一次进入 LLM 的请求将注入合成的首轮对话（含工具调用历史），"
            "且注入会随历史持久化；/new 后的新对话会自动重新注入。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("anchor_conv")
    async def bind_conv(self, event: AstrMessageEvent):
        """绑定当前数据库对话（UMO + conversation cid）：/new 后不自动延续。"""
        cid = None
        try:
            cid = await self.context.conversation_manager.get_curr_conversation_id(
                event.unified_msg_origin
            )
        except Exception as e:
            logger.warning(f"[{_PLUGIN}] get curr conversation id failed: {e}")
        await self._bind(event.unified_msg_origin, str(cid) if cid else None)
        yield event.plain_result(
            "已绑定当前对话。\n"
            f"UMO: {event.unified_msg_origin}\n"
            f"Conversation: {cid or '未获取到，按会话匹配'}\n"
            "下一次进入 LLM 的请求将注入合成的首轮对话。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("anchor_umo")
    async def bind_umo(self, event: AstrMessageEvent, umo: str = ""):
        """绑定指定 unified_msg_origin。用法：/anchor_umo platform:FriendMessage:xxx"""
        umo = (umo or "").strip()
        if not umo:
            yield event.plain_result("用法：/anchor_umo <unified_msg_origin>")
            return
        await self._bind(umo, None)
        yield event.plain_result(f"已绑定指定会话：{umo}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("anchor_off")
    async def unbind(self, event: AstrMessageEvent, target: str = ""):
        """解绑。/anchor_off 解绑当前会话；/anchor_off all 清空全部绑定。

        只解除绑定，不改动已经写入对话历史的合成首轮。
        """
        target = (target or "").strip()
        if target == "all":
            async with self._lock:
                data = await self._load()
                n = len(data["targets"])
                data["targets"] = {}
                await self._save(data)
            yield event.plain_result(f"已清空 {n} 个绑定。")
            return
        ok = await self._unbind(event.unified_msg_origin)
        yield event.plain_result("已解绑当前会话。" if ok else "当前会话本就没有绑定。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("anchor_status")
    async def status(self, event: AstrMessageEvent):
        """查看绑定状态。"""
        async with self._lock:
            data = await self._load()
        if not data["targets"]:
            yield event.plain_result("暂无绑定。")
            return
        lines = ["AnchoredRouter 绑定列表："]
        for key, tgt in data["targets"].items():
            lines.append(
                f"- {key}\n  updated_at={tgt.get('updated_at')} last_hit_at={tgt.get('last_hit_at')}"
            )
        yield event.plain_result("\n".join(lines))

    @filter.on_llm_tool_respond()
    async def on_llm_tool_respond(
        self,
        event: AstrMessageEvent,
        tool: Any,
        tool_args: dict | None,
        tool_result: Any,
    ) -> None:
        """minimal_promote 的 loop 内晋升点。

        AstrBot 的 on_llm_request 每条用户消息只触发一次，agent loop 内的
        后续 LLM 调用不经过插件；但 runner 全程复用同一个 ProviderRequest /
        ToolSet 对象。因此：首次工具结果返回后（下一轮 LLM 请求之前）把
        on_llm_request 时备份的全量工具加回该 ToolSet（保留已在的 dsh
        bash/str_replace_editor）。工具更新 reminder 不由本钩子携带——它在
        工具消息序列化之后才触发；reminder 由 dsh 工具 handler 在返回文本
        尾部追加（见 _promotion_reminder_suffix），随下一轮请求送达（dsh
        cot-drip 同款落点）。此后会话保持全工具状态。
        """
        if self._current_strategy() != _STRATEGY_MINIMAL_PROMOTE:
            return
        data = await self._load()
        matched_key = None
        for key, tgt in data["targets"].items():
            if tgt.get("umo") == event.unified_msg_origin:
                matched_key = key
                break
        if matched_key is None:
            return
        if (data["targets"][matched_key].get("mode") or _MODE_MINIMAL) != _MODE_MINIMAL:
            return

        req = self._promote_req.pop(matched_key, None)
        backup = self._promote_backup.pop(matched_key, None)
        if req is not None and backup:
            try:
                tool_set = _ensure_tool_set(req)
                present = {t.name for t in tool_set.tools}
                restored = 0
                for t in backup:
                    if t.name not in present:
                        tool_set.add_tool(t)
                        restored += 1
                logger.info(
                    "[%s] minimal_promote: restored %s tools mid-loop: key=%s",
                    _PLUGIN,
                    restored,
                    matched_key,
                )
            except Exception as e:
                logger.error(f"[{_PLUGIN}] restore full tools failed: {e}")

        # reminder 已由工具 handler 在返回文本尾部携带（本钩子在工具消息
        # 序列化之后才触发，改 tool_result 不会进入上下文，故不在此追加）。
        _PROMOTE_PENDING.discard(event.unified_msg_origin)

        async with self._lock:
            data = await self._load()
            if matched_key in data["targets"]:
                data["targets"][matched_key]["mode"] = _MODE_FULL
                data["targets"][matched_key]["updated_at"] = _now()
                await self._save(data)
        logger.info(
            "[%s] minimal_promote: promoted to full tool surface: key=%s",
            _PLUGIN,
            matched_key,
        )

    @filter.on_llm_request(priority=-1_000_000)
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """在绝大多数插件 hook 之后执行：为绑定会话注入 dsh 极简首轮环境。

        三部分注入（reminder/register/minimal 策略，仅绑定会话生效）：
        1. system：dsh 极简 system 行前置到 req.system_prompt 最前，并写入
           persona 区域；每轮幂等执行（AstrBot 每轮重组 system prompt）。
        2. 虚拟首轮：seed（含工具调用历史）插到 req.contexts 最前端，不带
           _no_save，随本轮持久化；后续轮次经 _SEED_ID_PREFIX 识别不重复注入。
        3. 仅在注入轮，向当前用户消息追加工具集变更的 <system_reminder>。
        tool_strategy=register 时额外把 dsh 极简版 bash/str_replace_editor
        （真实可执行实现）加进本轮 req.func_tool；minimal 时只保留这两个。

        1.1.2 新增（不做任何 seed 注入，仅操作 system 字段与 tools 列表）：
        - minimal_lean（1.1.2a）：system 行注入 + 永久仅 bash/str_replace_editor；
          system prompt 中点名原版工具的段落改写为两件套版本。
        - minimal_promote（1.1.2b）：minimal 面起步；模型首次调用工具后由
          on_llm_tool_respond 在 loop 内下一轮请求前恢复全量工具（dsh 两件套
          保留），reminder 追加在工具结果尾部；之后保持全量 + 两件套。
        """
        data = await self._load()
        if not data["targets"]:
            return
        matched_key = None
        for key, tgt in data["targets"].items():
            if self._match(tgt, event, req):
                matched_key = key
                break
        if matched_key is None:
            return

        strategy = self._current_strategy()

        # Make the session workspace path available to the dsh tool handlers
        # (their default cwd must be the workspace, not the AstrBot process
        # cwd). Extract BEFORE any prose rewrite; the rewritten workspace
        # section keeps the path, so later rounds keep refreshing it.
        m_ws = _WS_PATH_RE.search(req.system_prompt or "")
        if m_ws:
            _WORKSPACE_BY_UMO[event.unified_msg_origin] = m_ws.group(1)

        # Part 1: system prompt (front + persona area), idempotent, every round.
        _apply_dsh_system_line(req)

        # 1.1.2a: minimal_lean — system-line injection plus the permanent
        # two-tool minimal surface, no synthetic first round at all.
        if strategy == _STRATEGY_MINIMAL_LEAN:
            try:
                _apply_minimal_strategy(req)
            except Exception as e:
                logger.error(f"[{_PLUGIN}] apply minimal_lean strategy failed: {e}")
            return

        # 1.1.2b: minimal_promote — minimal surface until the model's first
        # tool call; the NEXT in-loop LLM call already carries the full
        # catalog (promotion happens in on_llm_tool_respond), and the session
        # stays full afterwards with the dsh pair retained.
        if strategy == _STRATEGY_MINIMAL_PROMOTE:
            mode = tgt.get("mode") or _MODE_MINIMAL
            if mode == _MODE_FULL:
                # promoted: full AstrBot catalog (reassembled by AstrBot each
                # round) plus the retained dsh pair.
                _PROMOTE_PENDING.discard(event.unified_msg_origin)
                try:
                    tool_set = _ensure_tool_set(req)
                    for tool in _build_dsh_tools():
                        tool_set.add_tool(tool)
                except Exception as e:
                    logger.error(f"[{_PLUGIN}] register dsh tools (full) failed: {e}")
                return
            try:
                tool_set = _ensure_tool_set(req)
                # Back up the full catalog BEFORE narrowing so the
                # on_llm_tool_respond hook can restore it mid-loop upon the
                # model's first tool call.
                self._promote_backup[matched_key] = list(tool_set.tools)
                self._promote_req[matched_key] = req
                # 标记该会话等待晋升：首次工具调用（必为 dsh 两件套之一）的
                # handler 会在返回文本尾部追加工具更新 reminder。
                _PROMOTE_PENDING.add(event.unified_msg_origin)
                _apply_minimal_strategy(req)
            except Exception as e:
                logger.error(f"[{_PLUGIN}] apply minimal_promote strategy failed: {e}")
            return

        # v2: register the migrated dsh Minimal tools for this request.
        if strategy == _STRATEGY_REGISTER:
            try:
                tool_set = _ensure_tool_set(req)
                for tool in _build_dsh_tools():
                    tool_set.add_tool(tool)
            except Exception as e:
                logger.error(f"[{_PLUGIN}] register dsh tools failed: {e}")

        # minimal: keep ONLY the dsh Minimal tools and scrub tool-naming prose.
        if strategy == _STRATEGY_MINIMAL:
            try:
                _apply_minimal_strategy(req)
            except Exception as e:
                logger.error(f"[{_PLUGIN}] apply minimal strategy failed: {e}")

        contexts = req.contexts or []
        if _has_seed(contexts):
            return
        try:
            seed = copy.deepcopy(_load_seed())
        except Exception as e:
            logger.error(f"[{_PLUGIN}] load seed failed: {e}")
            return
        req.contexts = [*seed, *contexts]

        # Part 3: tool-update reminder on the injection round only; it merges
        # into the current user message and persists with the history.
        try:
            req.extra_user_content_parts.append(TextPart(text=_TOOL_UPDATE_REMINDER))
        except Exception as e:
            logger.error(f"[{_PLUGIN}] append tool-update reminder failed: {e}")

        async with self._lock:
            data = await self._load()
            if matched_key in data["targets"]:
                data["targets"][matched_key]["last_hit_at"] = _now()
                data["targets"][matched_key]["updated_at"] = _now()
                await self._save(data)
        logger.info(
            "[%s] anchored warm-up injected: key=%s seed_messages=%s strategy=%s",
            _PLUGIN,
            matched_key,
            len(seed),
            strategy,
        )
