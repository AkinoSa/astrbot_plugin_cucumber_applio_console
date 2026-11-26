"""基于 LRC 时间戳歌词重复度的副歌起点定位（方案 2）。

工作原理
========
流行/ACG 等常见歌曲的副歌往往以「2 句以上连续歌词在 >=22s 跨度外重复出现」为特征。
本模块在翻唱提交给中间层前，按以下步骤尝试锁定第一段完整副歌的人声起唱点：

1. 优先用网易云歌曲 id（或按歌名/歌手快速检索匹配到的网易云 id）请求 LRC 歌词；
2. 清洗制作人/乐手/纯音乐标记，展开多时间戳行并合并双语同时间戳翻译行；
3. 统计每句歌词在 >=22s 跨度之外的重复出现次数，定位含 2~3 句以上连续重复歌词的
   「副歌起始句」（避免误抓连续复读的 ad-lib 或单句口号），优先选择第一段完整副歌；
4. 自动在起唱时间前留 ~0.35s 换气余量作为 `start_time` 传给云端/本地后端；
5. 若无歌词、纯音乐、超时或无可信重复段落，则返回 None，由后端声学算法接管。
"""

from __future__ import annotations

import asyncio
import re
import urllib.parse
from typing import Any, Optional

from astrbot.api import logger

try:
    from . import source_router
except ImportError:
    import source_router  # type: ignore[no-redef]


_LINE_TS_RE = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_OFFSET_RE = re.compile(r"\[offset:\s*([+-]?\d+)\s*\]", re.IGNORECASE)
_KARAOKE_RE = re.compile(r"<\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?>")
_NORM_RE = re.compile(r"[\s\W_]+")
_CREDIT_LINE_RE = re.compile(
    r"(?:作词|作曲|编曲|填词|谱曲|混音|母带|制作人|统筹|监制|出品|发行|策划|"
    r"吉他|贝斯|鼓手|架子鼓|键盘|钢琴|弦乐|和声|伴唱|录音|混缩|修音|人声编辑|"
    r"Vocal|Lyricist|Lyrics|Composer|Arranger|Producer|Mixing|Mastering|Guitar|Bass|Drum|"
    r"Translation|Translator|OP|SP)\s*[:：\-]",
    re.IGNORECASE,
)
_NOISE_TEXT_RE = re.compile(
    r"^(?:纯音乐|请欣赏|无歌词|间奏|前奏|尾奏|music|instrumental|solo|~+|—+|-+|\.+)$",
    re.IGNORECASE,
)


def _normalize_lyric(text: str) -> str:
    return _NORM_RE.sub("", str(text or "").casefold())


def _is_credit_or_noise(time_sec: float, raw_text: str) -> bool:
    text = str(raw_text or "").strip()
    if not text:
        return True
    if _NOISE_TEXT_RE.match(text):
        return True
    if _CREDIT_LINE_RE.search(text):
        return True
    if time_sec < 35.0 and (":" in text or "：" in text):
        head = text.split("：" if "：" in text else ":", 1)[0].strip()
        if 1 <= len(head) <= 8:
            return True
    return False


def parse_lrc(lrc_text: str) -> list[tuple[float, str]]:
    """把 LRC 文本解析为按时间升序排列的 ``[(seconds, text), ...]``，已合并双语同时间戳行。"""
    return [(t, texts[0]) for t, texts in _parse_lrc_slots(lrc_text)]


def _parse_lrc_slots(lrc_text: str) -> list[tuple[float, list[str]]]:
    """解析 LRC 并保留同一时间戳下的全部变体文本（用于双语歌词重复判定）。"""
    if not lrc_text or not isinstance(lrc_text, str):
        return []
    offset_sec = 0.0
    m_off = _OFFSET_RE.search(lrc_text)
    if m_off:
        try:
            offset_sec = float(int(m_off.group(1))) / 1000.0
        except (TypeError, ValueError):
            offset_sec = 0.0
    parsed: list[tuple[float, str]] = []
    for raw_line in lrc_text.splitlines():
        matches = list(_LINE_TS_RE.finditer(raw_line))
        if not matches:
            continue
        lyric = _LINE_TS_RE.sub("", raw_line)
        lyric = _KARAOKE_RE.sub("", lyric).strip()
        if not lyric:
            continue
        for m in matches:
            mins = int(m.group(1))
            secs = int(m.group(2))
            frac = m.group(3) or "0"
            if len(frac) == 1:
                ms = int(frac) * 100
            elif len(frac) == 2:
                ms = int(frac) * 10
            else:
                ms = int(frac[:3])
            t = max(0.0, mins * 60.0 + secs + ms / 1000.0 - offset_sec)
            if not _is_credit_or_noise(t, lyric):
                parsed.append((t, lyric))
    if not parsed:
        return []
    parsed.sort(key=lambda item: item[0])
    grouped: list[tuple[float, list[str]]] = []
    for t, text in parsed:
        if grouped and abs(t - grouped[-1][0]) <= 0.18:
            if text not in grouped[-1][1]:
                grouped[-1][1].append(text)
            continue
        grouped.append((t, [text]))
    return grouped


def _lines_match(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if len(a) >= 3 and a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) >= 6 and len(shorter) / max(len(longer), 1) >= 0.78 and shorter in longer:
        return True
    return False


def detect_chorus_from_lrc(
    lrc_text: str,
    total_dur: float = 0.0,
    target_len: float = 25.0,
) -> Optional[dict[str, Any]]:
    """从 LRC 歌词中识别第一段完整重复副歌的起唱点，返回 ``None`` 表示无可靠结论。"""
    slots = _parse_lrc_slots(lrc_text)
    valid: list[tuple[float, str, list[str]]] = []
    for t, texts in slots:
        pairs = [(txt, _normalize_lyric(txt)) for txt in texts]
        pairs = [(txt, nm) for txt, nm in pairs if len(nm) >= 2]
        if pairs:
            valid.append((t, pairs[0][0], [nm for _, nm in pairs]))
    if len(valid) < 8:
        return None

    times = [item[0] for item in valid]
    eff_total = max(float(total_dur or 0.0), times[-1] + 15.0)
    n = len(valid)

    # 统计每个时间槽在 >=22s 跨度之外出现过的次数（排除相邻复读词，兼容同时间戳双语行）
    rep_counts = [0] * n
    for i in range(n):
        nms_i = [nm for nm in valid[i][2] if len(nm) >= 3]
        if not nms_i:
            continue
        hits = 0
        for j in range(n):
            if i == j or abs(times[j] - times[i]) < 22.0:
                continue
            if any(_lines_match(a, b) for a in nms_i for b in valid[j][2]):
                hits += 1
        rep_counts[i] = hits

    if sum(1 for c in rep_counts if c > 0) < 3:
        return None

    intro_floor = 15.0 if eff_total >= 90.0 else eff_total * 0.12
    min_remain = min(max(12.0, float(target_len) * 0.75), max(12.0, eff_total * 0.35))
    win_span = max(float(target_len), 20.0) + 4.0

    best_any: Optional[dict[str, Any]] = None
    candidates: list[dict[str, Any]] = []

    for i in range(n):
        if rep_counts[i] <= 0:
            continue
        t_start = times[i]
        if eff_total - t_start < min_remain:
            continue

        # 收集从 i 起一个副歌窗口内的连续歌词（遇 >12s 长间奏截断）
        win_indices = [i]
        for k in range(i + 1, n):
            if times[k] - times[k - 1] > 12.0:
                break
            if times[k] - t_start > win_span and len(win_indices) >= 3:
                break
            win_indices.append(k)

        rep_in_win = sum(1 for k in win_indices if rep_counts[k] > 0)
        if rep_in_win < 2:
            continue
        rep_ratio = rep_in_win / max(len(win_indices), 1)
        if rep_in_win < 3 and rep_ratio < 0.75:
            continue
        if rep_ratio < 0.48:
            continue

        # 段落起手加权：前一句不是重复句，或前面有较大停顿，说明 i 才是副歌第一句
        onset_bonus = 1.0
        if i == 0 or rep_counts[i - 1] == 0:
            onset_bonus += 0.35
        elif (t_start - times[i - 1]) >= 2.2:
            onset_bonus += 0.25
        else:
            # 若紧跟在前一句也是重复句之后，适当降权防止滑到副歌第二句
            onset_bonus *= 0.72

        intro_penalty = 0.65 if t_start < intro_floor else 1.0
        base_score = rep_in_win * (0.55 + 0.45 * rep_ratio) * onset_bonus * intro_penalty

        # 第一完整副歌区间（16%~58% 曲长，且不触及尾奏）给予温和优先权
        pos_ratio = t_start / max(eff_total, 1.0)
        first_chorus_bonus = (
            1.14
            if (0.16 <= pos_ratio <= 0.58 and t_start >= intro_floor and (eff_total - t_start) >= float(target_len))
            else 1.0
        )

        cand = {
            "idx": i,
            "entry_sec": t_start,
            "sample_lyric": valid[i][1],
            "repeated_lines": rep_in_win,
            "window_lines": len(win_indices),
            "rep_ratio": round(rep_ratio, 3),
            "base_score": base_score,
            "score": base_score * first_chorus_bonus,
        }
        candidates.append(cand)
        if best_any is None or cand["score"] > best_any["score"]:
            best_any = cand

    if best_any is None:
        return None

    chosen = best_any
    # 同一副歌段内只应取起唱句：如果在 chosen 前 8s 内存在同类起手句，回锁到该段第一句
    for cand in candidates:
        if (
            0.0 < (chosen["entry_sec"] - cand["entry_sec"]) <= 8.0
            and (cand["idx"] == 0 or rep_counts[cand["idx"] - 1] == 0)
            and cand["repeated_lines"] >= max(2, chosen["repeated_lines"] - 1)
        ):
            chosen = cand
            break

    idx = chosen["idx"]
    t_entry = chosen["entry_sec"]
    prev_gap = (t_entry - times[idx - 1]) if idx > 0 else t_entry
    if idx > 0 and prev_gap < 1.2:
        preroll = min(0.25, prev_gap * 0.3)
    else:
        preroll = 0.35
    anchor = max(0.0, t_entry - preroll)

    return {
        "start_sec": round(anchor, 2),
        "entry_sec": round(t_entry, 2),
        "sample_lyric": chosen["sample_lyric"],
        "repeated_lines": chosen["repeated_lines"],
        "rep_ratio": chosen["rep_ratio"],
        "score": round(chosen["score"], 3),
    }


def _extract_lrc_text(payload: Any) -> str:
    if isinstance(payload, str):
        return payload if _LINE_TS_RE.search(payload) else ""
    if isinstance(payload, dict):
        if isinstance(payload.get("lrc"), dict) and payload["lrc"].get("lyric"):
            return str(payload["lrc"]["lyric"])
        for key in ("lyric", "lrc", "text"):
            val = payload.get(key)
            if isinstance(val, str) and _LINE_TS_RE.search(val):
                return val
    return ""


async def _fetch_lrc_by_netease_id(netease_fetcher: Any, song_id: str) -> str:
    song_id = str(song_id or "").strip()
    if not song_id.isdigit() or netease_fetcher is None:
        return ""
    meting_base = str(getattr(netease_fetcher, "meting_base", "") or "").strip()
    if meting_base:
        sep = "&" if "?" in meting_base else "?"
        url = f"{meting_base}{sep}type=lrc&id={urllib.parse.quote(song_id)}"
        text = _extract_lrc_text(await netease_fetcher._request(url, attempts=1))
        if text:
            return text
    api_base = str(getattr(netease_fetcher, "api_base", "") or "").strip()
    if api_base:
        url = f"{api_base.rstrip('/')}/lyric?id={urllib.parse.quote(song_id)}"
        text = _extract_lrc_text(await netease_fetcher._request(url, attempts=1))
        if text:
            return text
    return ""


async def locate_chorus_from_song(
    netease_fetcher: Any,
    song_info: Optional[dict],
    *,
    resolved_meta: Optional[dict] = None,
    target_len: float = 25.0,
    total_dur: float = 0.0,
    timeout: float = 4.0,
) -> Optional[dict[str, Any]]:
    """快速查找歌曲 LRC 歌词并定位副歌起点；超时或无可靠段落一律返回 None。"""
    if netease_fetcher is None:
        return None
    meta = resolved_meta if isinstance(resolved_meta, dict) else (song_info if isinstance(song_info, dict) else {})
    song_id = str(meta.get("id") or (song_info or {}).get("id") or "").strip()
    source = str(meta.get("source") or (song_info or {}).get("source") or "").strip().casefold()
    name = str(meta.get("name") or (song_info or {}).get("name") or "").strip()
    artists = str(meta.get("artists") or (song_info or {}).get("artists") or "").strip()
    dur_ms = meta.get("duration_ms") or (song_info or {}).get("duration") or 0
    if not total_dur and dur_ms:
        try:
            total_dur = float(dur_ms) / 1000.0
        except (TypeError, ValueError):
            total_dur = 0.0

    async def _run() -> Optional[dict[str, Any]]:
        lrc_text = ""
        if song_id.isdigit() and source in ("", "netease"):
            lrc_text = await _fetch_lrc_by_netease_id(netease_fetcher, song_id)
        if not lrc_text and name:
            # 跨平台音源（QQ/酷狗/酷我等）按歌名+歌手补充匹配网易云歌词
            raw_cands = await netease_fetcher.search_songs(name, limit=5)
            cands = [
                c for c in (source_router.candidate_from_dict(item, source="netease") for item in raw_cands)
                if c is not None
            ]
            if cands:
                picked, _status, _stats = source_router.pick_best_song(cands, name, artists)
                if picked is None:
                    picked = cands[0]
                if not total_dur and picked.duration_ms:
                    total_eff = picked.duration_ms / 1000.0
                else:
                    total_eff = total_dur
                lrc_text = await _fetch_lrc_by_netease_id(netease_fetcher, picked.id)
                if lrc_text:
                    return detect_chorus_from_lrc(lrc_text, total_dur=total_eff, target_len=target_len)
        if lrc_text:
            return detect_chorus_from_lrc(lrc_text, total_dur=total_dur, target_len=target_len)
        return None

    try:
        return await asyncio.wait_for(_run(), timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[ChorusLyric] 歌词副歌定位跳过: {type(exc).__name__}: {exc}")
        return None
