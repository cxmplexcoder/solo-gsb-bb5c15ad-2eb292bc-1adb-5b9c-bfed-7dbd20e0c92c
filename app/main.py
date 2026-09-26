"""访谈字幕时间轴校准与锚点推荐 API。

- ``POST /anchors/suggest``：接收原片与成片两条按开始时间严格递增的字幕序列，
  在允许跳过片段的前提下做全局最优的文字精确匹配，返回可直接传给
  ``/calibrate`` 的同步锚点。
- ``POST /calibrate``：客户端提交以原录音毫秒计时的字幕片段，以及至少两组
  "原时间 -> 成片时间" 同步锚点；服务端在相邻锚点间做线性换算，
  并对超出锚点覆盖范围、换算后时长非正的片段返回错误。
- ``POST /layout/wrap``：接收字幕片段与行宽、行数约束，按 Unicode 码点显示
  宽度（东亚全角/宽字符 2 格，其余 1 格）为每段做全局最优断行，保留原时码
  与文字，返回各行文字与断行字符偏移；无法排版或片段时长非正时返回 422
  并指出字幕下标。
- ``POST /edits/project``：接收源字幕与按成片顺序排列的剪辑段（允许源区间
  重叠、重排、重复使用），按半开区间求每条字幕与每段剪辑的交集，再按剪辑
  时长与源区间长度之比线性映射到成片时间轴；精确计算后四舍五入到毫秒，
  丢弃舍入后零时长片段，返回可直接送入 ``/layout/wrap`` 的片段及未产生输出
  的字幕下标。
"""
from __future__ import annotations

import json
import unicodedata
from array import array
from bisect import bisect_right
from fractions import Fraction
from typing import List, Optional, Tuple

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #
class Anchor(BaseModel):
    """一组同步锚点：原录音时间 -> 成片时间（单位：毫秒）。"""

    original: int = Field(..., description="原录音时间（毫秒）")
    final: int = Field(..., description="成片时间（毫秒）")


class Segment(BaseModel):
    """一条以原录音毫秒计时的字幕片段。"""

    start: int = Field(..., description="片段原起始时间（毫秒）")
    end: int = Field(..., description="片段原结束时间（毫秒）")
    text: str = Field("", description="字幕文字")


class CalibrateRequest(BaseModel):
    anchors: List[Anchor] = Field(
        ..., description="同步锚点，至少两组；两列时间均须严格递增"
    )
    segments: List[Segment] = Field(..., description="待校准的字幕片段")

    @field_validator("segments")
    @classmethod
    def _segments_not_empty(cls, v: List[Segment]) -> List[Segment]:
        if not v:
            raise ValueError("segments 不能为空")
        return v


class CalibratedSegment(BaseModel):
    index: int = Field(..., description="片段在输入中的位置（从 0 开始）")
    start: int = Field(..., description="换算后起始时间（毫秒，整数）")
    end: int = Field(..., description="换算后结束时间（毫秒，整数）")
    text: str = Field("", description="原字幕文字")


class CalibrateResponse(BaseModel):
    segments: List[CalibratedSegment]


class SubtitleCue(BaseModel):
    """一条字幕：整数毫秒起始时间与文字。"""

    start: int = Field(..., description="起始时间（毫秒）")
    text: str = Field(..., description="字幕文字")


class SuggestAnchorsRequest(BaseModel):
    original: List[SubtitleCue] = Field(
        ..., description="原片字幕序列，start 须为非负整数且严格递增"
    )
    final: List[SubtitleCue] = Field(
        ..., description="成片字幕序列，start 须为非负整数且严格递增"
    )


class SuggestedAnchor(BaseModel):
    """一组推荐锚点及其来源下标，可原样作为 /calibrate 的锚点。"""

    original: int = Field(..., description="原片字幕起始时间（毫秒）")
    final: int = Field(..., description="成片字幕起始时间（毫秒）")
    original_index: int = Field(..., description="原片字幕下标（从 0 开始）")
    final_index: int = Field(..., description="成片字幕下标（从 0 开始）")


class SuggestAnchorsResponse(BaseModel):
    anchors: List[SuggestedAnchor]


class ErrorItem(BaseModel):
    location: str = Field(..., description="出错位置，如 anchor / segment")
    index: Optional[int] = Field(None, description="出错元素下标（从 0 开始）")
    code: str = Field(..., description="错误代码")
    message: str = Field(..., description="错误说明")


class WrapSegment(BaseModel):
    """一条待断行排版的字幕片段。"""

    start: int = Field(..., description="片段起始时间（毫秒）")
    end: int = Field(..., description="片段结束时间（毫秒）")
    text: str = Field(..., description="字幕文字")


class WrapRequest(BaseModel):
    segments: List[WrapSegment] = Field(..., description="待断行排版的字幕片段")
    max_width: int = Field(..., gt=0, description="每行最大显示宽度（格），正整数")
    max_lines: int = Field(..., ge=1, le=4, description="每段最多行数，1~4")

    @field_validator("segments")
    @classmethod
    def _segments_not_empty(cls, v: List[WrapSegment]) -> List[WrapSegment]:
        if not v:
            raise ValueError("segments 不能为空")
        return v


class WrappedSegment(BaseModel):
    index: int = Field(..., description="片段在输入中的位置（从 0 开始）")
    start: int = Field(..., description="原片段起始时间（毫秒）")
    end: int = Field(..., description="原片段结束时间（毫秒）")
    text: str = Field(..., description="原字幕文字")
    lines: List[str] = Field(..., description="断行后的各行文字")
    breaks: List[int] = Field(
        ..., description="断行字符偏移：在原文第 i 个码点（从 0 计）之前断行，升序"
    )


class WrapResponse(BaseModel):
    segments: List[WrappedSegment]


class ProjectSegment(BaseModel):
    """一条待投影的字幕（源时间轴，整数毫秒）。"""

    start: int = Field(..., description="字幕起始时间（源时间轴，毫秒）")
    end: int = Field(..., description="字幕结束时间（源时间轴，毫秒，半开区间）")
    text: str = Field("", description="字幕文字")


class EditClip(BaseModel):
    """一段剪辑：源时间轴半开区间与它在成片中占用的时长（均为整数毫秒）。"""

    source_start: int = Field(..., description="源区间起始时间（毫秒）")
    source_end: int = Field(..., description="源区间结束时间（毫秒，半开区间）")
    duration: int = Field(..., description="该段在成片时间轴上占用的时长（毫秒，正整数）")


class ProjectEditsRequest(BaseModel):
    segments: List[ProjectSegment] = Field(..., description="待投影的源字幕片段")
    clips: List[EditClip] = Field(
        ..., description="按成片顺序排列的剪辑段；允许源区间重叠、重排与重复使用"
    )


class ProjectedSegment(BaseModel):
    """投影到成片时间轴后的字幕片段。

    除两个来源下标外，字段与 ``/layout/wrap`` 的输入元素一致，可直接随
    ``max_width`` / ``max_lines`` 送入 ``/layout/wrap``（多余字段会被忽略）。
    """

    start: int = Field(..., description="成片时间轴起始时间（毫秒，整数）")
    end: int = Field(..., description="成片时间轴结束时间（毫秒，整数）")
    text: str = Field("", description="原字幕文字")
    segment_index: int = Field(
        ..., description="来源字幕在输入 segments 中的下标（从 0 开始）"
    )
    clip_index: int = Field(
        ..., description="来源剪辑在输入 clips 中的下标（从 0 开始，即成片顺序）"
    )


class ProjectEditsResponse(BaseModel):
    segments: List[ProjectedSegment]
    unused_segment_indices: List[int] = Field(
        ..., description="未在任何剪辑中产生有效输出的字幕下标（升序）"
    )


# --------------------------------------------------------------------------- #
# 核心换算逻辑
# --------------------------------------------------------------------------- #
def round_half_up(value: Fraction) -> int:
    """四舍五入到整数（.5 一律向远离零方向进位）。"""
    q, r = divmod(value.numerator, value.denominator)
    if r * 2 >= value.denominator:
        q += 1
    return q


def build_anchor_lines(anchors: List[Anchor]) -> List[tuple]:
    """把相邻锚点预编译为 (o0, f0, slope) 线段，slope 为精确分数。"""
    lines = []
    for a, b in zip(anchors, anchors[1:]):
        slope = Fraction(b.final - a.final, b.original - a.original)
        lines.append((a.original, a.final, slope))
    return lines


def map_time(
    t: int,
    originals: List[int],
    finals: List[int],
    lines: List[tuple],
) -> Optional[Fraction]:
    """把单个原时间线性换算为成片时间；超出锚点覆盖范围返回 None。"""
    first, last = originals[0], originals[-1]
    if t < first or t > last:
        return None
    # 最后一个锚点原样命中；其余时间点归入其左侧锚点所在线段
    i = bisect_right(originals, t) - 1
    if i == len(originals) - 1:
        return Fraction(finals[-1])
    o0, f0, slope = lines[i]
    return f0 + slope * (t - o0)


def validate_anchors(anchors: List[Anchor]) -> List[ErrorItem]:
    """校验锚点数量及两列时间严格递增。"""
    errors: List[ErrorItem] = []
    if len(anchors) < 2:
        errors.append(
            ErrorItem(
                location="anchors",
                index=None,
                code="not_enough_anchors",
                message="至少需要两组同步锚点",
            )
        )
        return errors

    for i in range(1, len(anchors)):
        if anchors[i].original <= anchors[i - 1].original:
            errors.append(
                ErrorItem(
                    location="anchor.original",
                    index=i,
                    code="not_strictly_increasing",
                    message=(
                        f"锚点 original 未严格递增：第 {i} 组为 {anchors[i].original}，"
                        f"前一组为 {anchors[i - 1].original}"
                    ),
                )
            )
        if anchors[i].final <= anchors[i - 1].final:
            errors.append(
                ErrorItem(
                    location="anchor.final",
                    index=i,
                    code="not_strictly_increasing",
                    message=(
                        f"锚点 final 未严格递增：第 {i} 组为 {anchors[i].final}，"
                        f"前一组为 {anchors[i - 1].final}"
                    ),
                )
            )
    return errors


def calibrate(req: CalibrateRequest) -> tuple[Optional[CalibrateResponse], List[ErrorItem]]:
    """执行校准；成功返回响应，失败返回错误列表。"""
    errors = validate_anchors(req.anchors)
    if errors:
        return None, errors

    lines = build_anchor_lines(req.anchors)
    originals = [a.original for a in req.anchors]
    finals = [a.final for a in req.anchors]
    results: List[CalibratedSegment] = []

    for idx, seg in enumerate(req.segments):
        if seg.end <= seg.start:
            errors.append(
                ErrorItem(
                    location="segment",
                    index=idx,
                    code="non_positive_duration",
                    message=(
                        f"原片段时长非正：start={seg.start}，end={seg.end}，要求 end > start"
                    ),
                )
            )
            continue

        start_mapped = map_time(seg.start, originals, finals, lines)
        end_mapped = map_time(seg.end, originals, finals, lines)
        if start_mapped is None or end_mapped is None:
            lo, hi = req.anchors[0].original, req.anchors[-1].original
            errors.append(
                ErrorItem(
                    location="segment",
                    index=idx,
                    code="out_of_anchor_range",
                    message=(
                        f"第 {idx} 段 [{seg.start}, {seg.end}] 超出锚点覆盖范围 "
                        f"[{lo}, {hi}]"
                    ),
                )
            )
            continue

        if end_mapped <= start_mapped:
            errors.append(
                ErrorItem(
                    location="segment",
                    index=idx,
                    code="non_positive_mapped_duration",
                    message=(
                        f"第 {idx} 段换算后时长非正："
                        f"start={float(start_mapped):.3f}ms，end={float(end_mapped):.3f}ms"
                    ),
                )
            )
            continue

        start_ms = round_half_up(start_mapped)
        end_ms = round_half_up(end_mapped)
        if end_ms <= start_ms:
            errors.append(
                ErrorItem(
                    location="segment",
                    index=idx,
                    code="non_positive_mapped_duration_after_rounding",
                    message=(
                        f"第 {idx} 段四舍五入后时长非正：start={start_ms}，end={end_ms}"
                    ),
                )
            )
            continue

        results.append(
            CalibratedSegment(
                index=idx, start=start_ms, end=end_ms, text=seg.text
            )
        )

    if errors:
        return None, errors
    return CalibrateResponse(segments=results), []


# --------------------------------------------------------------------------- #
# 锚点推荐逻辑
# --------------------------------------------------------------------------- #
def normalize_text(text: str) -> str:
    """去首尾空白，并把连续空白（含全角空格等 Unicode 空白）统一为单个空格。"""
    return " ".join(text.split())


def validate_subtitle_track(
    cues: List[SubtitleCue], track: str
) -> List[ErrorItem]:
    """校验单条字幕轨道：start 非负、严格递增、文字去空白后非空。"""
    errors: List[ErrorItem] = []
    for idx, cue in enumerate(cues):
        if cue.start < 0:
            errors.append(
                ErrorItem(
                    location=f"{track}.start",
                    index=idx,
                    code="negative_time",
                    message=f"{track} 第 {idx} 条 start 为负：{cue.start}",
                )
            )
        if idx > 0 and cue.start <= cues[idx - 1].start:
            errors.append(
                ErrorItem(
                    location=f"{track}.start",
                    index=idx,
                    code="not_strictly_increasing",
                    message=(
                        f"{track} start 未严格递增：第 {idx} 条为 {cue.start}，"
                        f"前一条为 {cues[idx - 1].start}"
                    ),
                )
            )
        if not normalize_text(cue.text):
            errors.append(
                ErrorItem(
                    location=f"{track}.text",
                    index=idx,
                    code="empty_text",
                    message=f"{track} 第 {idx} 条 text 去空白后为空",
                )
            )
    return errors


def best_alignment(a: List[str], b: List[str]) -> List[Tuple[int, int]]:
    """求两条文本序列允许跳过时的全局最优有序一对一匹配。

    依次优化：
      1. 匹配数最多；
      2. 配对序号差绝对值之和 ``sum(|i - j|)`` 最小；
      3. 配对序号序列 ``[(i, j), ...]`` 字典序最小（决定重复台词等平局）。

    用后缀动态规划：``F(i, j)`` 表示后缀 ``a[i:]``、``b[j:]`` 上的最优解，
    决策为匹配 (i, j)（仅当文本相同）、跳过 a[i] 或跳过 b[j]。每个单元存
    (匹配数, 代价, 第一个配对)；在匹配数与代价持平时比较候选后继的第一个配对
    ——第一个配对相同则整条配对序列完全相同。重建时，首配对为 (pi, pj) 的
    单元，其后续必然是 F(pi+1, pj+1)，沿此跳转即可。所有行使用定长数组保留，
    额外内存 O(n*m)。
    """
    n, m = len(a), len(b)

    all_count: List[array] = [array("i", [0]) * (m + 1)]
    all_cost: List[array] = [array("q", [0]) * (m + 1)]
    all_pi: List[array] = [array("i", [-1]) * (m + 1)]
    all_pj: List[array] = [array("i", [-1]) * (m + 1)]

    for i in range(n - 1, -1, -1):
        cur_count = array("i", [0]) * (m + 1)
        cur_cost = array("q", [0]) * (m + 1)
        cur_pi = array("i", [-1]) * (m + 1)
        cur_pj = array("i", [-1]) * (m + 1)
        nxt_count = all_count[-1]
        nxt_cost = all_cost[-1]
        nxt_pi = all_pi[-1]
        nxt_pj = all_pj[-1]
        ai = a[i]

        for j in range(m - 1, -1, -1):
            j1 = j + 1
            # 候选 1：跳过 b[j]，后继为同一行的 F(i, j+1)
            best_c = cur_count[j1]
            best_k = cur_cost[j1]
            best_pi = cur_pi[j1]
            best_pj = cur_pj[j1]

            # 候选 2：跳过 a[i]，后继为 F(i+1, j)
            c2, k2 = nxt_count[j], nxt_cost[j]
            p2i, p2j = nxt_pi[j], nxt_pj[j]
            if (
                c2 > best_c
                or c2 == best_c
                and (
                    k2 < best_k
                    or k2 == best_k
                    and (
                        # 平局比首配对；首配对为 -1 表示空序列，排在最后
                        p2i != -1
                        and (
                            best_pi == -1
                            or (p2i, p2j) < (best_pi, best_pj)
                        )
                    )
                )
            ):
                best_c, best_k = c2, k2
                best_pi, best_pj = p2i, p2j

            # 候选 3：匹配 (i, j)，后继为 F(i+1, j+1)
            if ai == b[j]:
                c3 = nxt_count[j1] + 1
                k3 = nxt_cost[j1] + abs(i - j)
                # 匹配候选在匹配数/代价持平时，首配对 (i, j) 必然最小
                if c3 > best_c or (c3 == best_c and k3 <= best_k):
                    best_c, best_k = c3, k3
                    best_pi, best_pj = i, j

            cur_count[j] = best_c
            cur_cost[j] = best_k
            cur_pi[j] = best_pi
            cur_pj[j] = best_pj

        all_count.append(cur_count)
        all_cost.append(cur_cost)
        all_pi.append(cur_pi)
        all_pj.append(cur_pj)

    # all_*[n - i] 保存的是行 F(i, *)；首配对 (pi, pj) 的后续即 F(pi+1,pj+1)
    pairs: List[Tuple[int, int]] = []
    i = j = 0
    while i < n and j < m:
        r = n - i
        pi, pj = all_pi[r][j], all_pj[r][j]
        if pi == -1:
            break
        pairs.append((pi, pj))
        i, j = pi + 1, pj + 1
    return pairs


def suggest_anchors(
    req: SuggestAnchorsRequest,
) -> tuple[Optional[SuggestAnchorsResponse], List[ErrorItem]]:
    """校验两条轨道并求全局最优匹配，生成可直接用于 /calibrate 的锚点。"""
    errors: List[ErrorItem] = []
    errors.extend(validate_subtitle_track(req.original, "original"))
    errors.extend(validate_subtitle_track(req.final, "final"))
    if errors:
        return None, errors

    original_text = [normalize_text(c.text) for c in req.original]
    final_text = [normalize_text(c.text) for c in req.final]
    pairs = best_alignment(original_text, final_text)

    if len(pairs) < 2:
        errors.append(
            ErrorItem(
                location="anchors",
                index=None,
                code="not_enough_matches",
                message=(
                    f"匹配片段仅 {len(pairs)} 对，至少需要 2 对才能构成同步锚点"
                ),
            )
        )
        return None, errors

    anchors = [
        SuggestedAnchor(
            original=req.original[i].start,
            final=req.final[j].start,
            original_index=i,
            final_index=j,
        )
        for i, j in pairs
    ]
    return SuggestAnchorsResponse(anchors=anchors), []


# --------------------------------------------------------------------------- #
# 字幕断行排版逻辑
# --------------------------------------------------------------------------- #
# 避头尾规则：行首不得出现以下标点，行尾不得为开括号
LINE_START_FORBIDDEN = frozenset("，。！？；：）】")
LINE_END_FORBIDDEN = frozenset("（【")


def display_width(ch: str) -> int:
    """单个 Unicode 码点的显示宽度：东亚全角/宽字符 2 格，其余 1 格。"""
    return 2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1


def wrap_text(
    text: str, max_width: int, max_lines: int
) -> Optional[Tuple[List[str], List[int]]]:
    """对单段文字求全局最优断行，返回 (各行文字, 断点字符偏移)；不可行返回 None。

    在所有满足约束（只在字符之间断行、行非空且不超 max_width、避头尾、
    行数不超过 max_lines）的方案中依次优化：
      1. 行数最少；
      2. 最大行宽最小；
      3. 各行宽平方和最小；
      4. 断点序列字典序最小。

    分四个阶段求解（动态规划或带交换论证的构造，而非逐行贪心）。
    """
    n = len(text)
    if n == 0:
        return None

    # 码点显示宽度的前缀和，prefix[e] - prefix[p] 即 text[p:e] 的行宽
    prefix = [0] * (n + 1)
    for i, ch in enumerate(text):
        prefix[i + 1] = prefix[i] + display_width(ch)

    # can_start[p]：允许一行从 p 开始；can_end[e]：允许一行在 e 结束
    can_start = [ch not in LINE_START_FORBIDDEN for ch in text]
    can_end = [False] * (n + 1)
    for e in range(1, n + 1):
        can_end[e] = text[e - 1] not in LINE_END_FORBIDDEN

    # 阶段 1：最少行数 k。每行尽量向右延展的贪心可证最优（交换论证：
    # 对任意可行解，贪心第 i 行的结束位置不早于它，故贪心行数 ≤ 任意可行解）。
    # 注意行尾 e 还须满足 e == n 或下一行能在 e 起始，否则贪心会越过可行断点。
    cuts = [0]
    while cuts[-1] < n:
        cur = cuts[-1]
        if not can_start[cur]:
            return None
        nxt = -1
        e = cur + 1
        while e <= n and prefix[e] - prefix[cur] <= max_width:
            if can_end[e] and (e == n or can_start[e]):
                nxt = e
            e += 1
        if nxt == -1:
            return None
        cuts.append(nxt)
    k = len(cuts) - 1
    if k > max_lines:
        return None

    # 阶段 2：恰好 k 行时最大行宽的最小值 M（min-max 动态规划，
    # f[j][i] 表示 text[:i] 排成 j 行时的最小最大行宽）
    INF = float("inf")
    f = [[INF] * (n + 1) for _ in range(k + 1)]
    f[0][0] = 0
    for j in range(1, k + 1):
        prev, cur_row = f[j - 1], f[j]
        for i in range(1, n + 1):
            if not can_end[i]:
                continue
            best = INF
            p = i - 1
            while p >= 0 and prefix[i] - prefix[p] <= max_width:
                if can_start[p] and prev[p] != INF:
                    w = prefix[i] - prefix[p]
                    cand = prev[p] if prev[p] > w else w
                    if cand < best:
                        best = cand
                p -= 1
            cur_row[i] = best
    limit = f[k][n]

    # 阶段 3：每行 ≤ M 时各行宽平方和的最小值（后缀动态规划，
    # G[j][i] 表示 text[i:] 排成 j 行的最小平方和）
    G = [[INF] * (n + 1) for _ in range(k + 1)]
    G[0][n] = 0
    for j in range(1, k + 1):
        prev, cur_row = G[j - 1], G[j]
        for i in range(n - 1, -1, -1):
            if not can_start[i]:
                continue
            best = INF
            e = i + 1
            while e <= n and prefix[e] - prefix[i] <= limit:
                if can_end[e] and prev[e] != INF:
                    w = prefix[e] - prefix[i]
                    cand = w * w + prev[e]
                    if cand < best:
                        best = cand
                e += 1
            cur_row[i] = best
    total = G[k][0]

    # 阶段 4：在达到最小平方和的方案中，从左到右逐步取可行的最小断点，
    # 得到字典序最小的断点序列。不变式：进入每轮时 budget 等于当前后缀
    # 在剩余行数下的最小平方和，故循环内必然找到可行断点（e ≤ n）。
    cuts = [0]
    budget = total
    for remaining in range(k - 1, -1, -1):
        cur = cuts[-1]
        e = cur + 1
        while True:
            w = prefix[e] - prefix[cur]
            if w > limit:
                break
            if (
                can_end[e]
                and G[remaining][e] != INF
                and w * w + G[remaining][e] <= budget
            ):
                cuts.append(e)
                budget -= w * w
                break
            e += 1

    breaks = cuts[1:-1]
    lines = [text[cuts[i] : cuts[i + 1]] for i in range(k)]
    return lines, breaks


def layout_wrap(req: WrapRequest) -> tuple[Optional[WrapResponse], List[ErrorItem]]:
    """逐段做全局最优断行；任一段失败则汇总全部错误返回。"""
    errors: List[ErrorItem] = []
    results: List[WrappedSegment] = []

    for idx, seg in enumerate(req.segments):
        if seg.end <= seg.start:
            errors.append(
                ErrorItem(
                    location="segment",
                    index=idx,
                    code="non_positive_duration",
                    message=(
                        f"片段时长非正：start={seg.start}，end={seg.end}，要求 end > start"
                    ),
                )
            )
            continue

        if not seg.text:
            errors.append(
                ErrorItem(
                    location="segment",
                    index=idx,
                    code="cannot_wrap",
                    message=f"第 {idx} 段文字为空，无法排版",
                )
            )
            continue

        wrapped = wrap_text(seg.text, req.max_width, req.max_lines)
        if wrapped is None:
            errors.append(
                ErrorItem(
                    location="segment",
                    index=idx,
                    code="cannot_wrap",
                    message=(
                        f"第 {idx} 段无法在 max_width={req.max_width}、"
                        f"max_lines={req.max_lines} 的约束下完成断行排版"
                    ),
                )
            )
            continue

        lines, breaks = wrapped
        results.append(
            WrappedSegment(
                index=idx,
                start=seg.start,
                end=seg.end,
                text=seg.text,
                lines=lines,
                breaks=breaks,
            )
        )

    if errors:
        return None, errors
    return WrapResponse(segments=results), []


# --------------------------------------------------------------------------- #
# 剪辑投影逻辑
# --------------------------------------------------------------------------- #
def validate_project_input(
    segments: List[ProjectSegment], clips: List[EditClip]
) -> List[ErrorItem]:
    """校验投影输入：列表非空、起点非负、各时长为正。错误一次性收集。"""
    errors: List[ErrorItem] = []

    if not segments:
        errors.append(
            ErrorItem(
                location="segments",
                index=None,
                code="empty",
                message="segments 不能为空",
            )
        )
    if not clips:
        errors.append(
            ErrorItem(
                location="clips",
                index=None,
                code="empty",
                message="clips 不能为空",
            )
        )

    for idx, seg in enumerate(segments):
        if seg.start < 0:
            errors.append(
                ErrorItem(
                    location="segments.start",
                    index=idx,
                    code="negative_time",
                    message=f"segments 第 {idx} 条 start 为负：{seg.start}",
                )
            )
        if seg.end - seg.start <= 0:
            errors.append(
                ErrorItem(
                    location="segments.end",
                    index=idx,
                    code="non_positive_duration",
                    message=(
                        f"segments 第 {idx} 条时长非正：start={seg.start}，"
                        f"end={seg.end}，要求 end > start"
                    ),
                )
            )

    for idx, clip in enumerate(clips):
        if clip.source_start < 0:
            errors.append(
                ErrorItem(
                    location="clips.source_start",
                    index=idx,
                    code="negative_time",
                    message=(
                        f"clips 第 {idx} 段 source_start 为负：{clip.source_start}"
                    ),
                )
            )
        if clip.source_end - clip.source_start <= 0:
            errors.append(
                ErrorItem(
                    location="clips.source_end",
                    index=idx,
                    code="non_positive_duration",
                    message=(
                        f"clips 第 {idx} 段源区间长度非正："
                        f"source_start={clip.source_start}，"
                        f"source_end={clip.source_end}，要求 source_end > source_start"
                    ),
                )
            )
        if clip.duration <= 0:
            errors.append(
                ErrorItem(
                    location="clips.duration",
                    index=idx,
                    code="non_positive_duration",
                    message=(
                        f"clips 第 {idx} 段 duration 非正：{clip.duration}，要求为正整数"
                    ),
                )
            )
    return errors


def project_edits(
    req: ProjectEditsRequest,
) -> tuple[Optional[ProjectEditsResponse], List[ErrorItem]]:
    """把源字幕按剪辑段投影到成片时间轴。

    第 j 段剪辑（按成片顺序）落在成片轴半开区间
    ``[cut_start_j, cut_start_j + duration_j)``，其中 ``cut_start_j`` 为前面
    各段 duration 的累计和；源区间允许重叠、重排与重复使用。对每条字幕与每段
    剪辑按半开区间求源时间交集，再按 ``duration / (source_end - source_start)``
    之比线性映射两个端点（精确分数），四舍五入到毫秒后丢弃零时长片段。
    """
    errors = validate_project_input(req.segments, req.clips)
    if errors:
        return None, errors

    results: List[ProjectedSegment] = []
    used = [False] * len(req.segments)

    cut_start = 0
    for clip_idx, clip in enumerate(req.clips):
        scale = Fraction(clip.duration, clip.source_end - clip.source_start)
        cut_end = cut_start + clip.duration
        for seg_idx, seg in enumerate(req.segments):
            # 半开区间 [seg.start, seg.end) ∩ [clip.source_start, clip.source_end)
            lo = max(seg.start, clip.source_start)
            hi = min(seg.end, clip.source_end)
            if lo >= hi:
                continue

            # 交集端点线性映射到成片轴：cut_start + scale * (t - source_start)
            mapped_lo = cut_start + scale * (lo - clip.source_start)
            mapped_hi = cut_start + scale * (hi - clip.source_start)
            start_ms = round_half_up(mapped_lo)
            end_ms = round_half_up(mapped_hi)
            if end_ms <= start_ms:
                # 舍入后零时长（或负时长）片段直接丢弃，不计为产生输出
                continue

            used[seg_idx] = True
            results.append(
                ProjectedSegment(
                    start=start_ms,
                    end=end_ms,
                    text=seg.text,
                    segment_index=seg_idx,
                    clip_index=clip_idx,
                )
            )
        cut_start = cut_end

    results.sort(
        key=lambda r: (r.start, r.end, r.segment_index, r.clip_index)
    )
    unused_indices = [i for i, is_used in enumerate(used) if not is_used]
    return (
        ProjectEditsResponse(
            segments=results, unused_segment_indices=unused_indices
        ),
        [],
    )


# --------------------------------------------------------------------------- #
# FastAPI
# --------------------------------------------------------------------------- #
app = FastAPI(
    title="访谈字幕时间轴校准 API",
    description=(
        "1. ``POST /anchors/suggest``：对比原片与成片两条字幕序列，"
        "全局最优匹配文字相同的片段，返回可直接使用的「原时间 -> 成片时间」"
        "同步锚点；\n"
        "2. ``POST /calibrate``：根据至少两组同步锚点，在相邻锚点间线性换算"
        "字幕片段的起止时间（毫秒）；\n"
        "3. ``POST /layout/wrap``：按显示宽度（全角/宽字符 2 格、其余 1 格）"
        "为字幕片段做全局最优断行，返回各行文字与断行字符偏移；\n"
        "4. ``POST /edits/project``：把源字幕按剪辑段（允许源区间重叠、重排、"
        "重复使用）线性投影到成片时间轴，返回可直接送入 /layout/wrap 的片段。"
    ),
    version="1.3.0",
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/anchors/suggest", response_model=SuggestAnchorsResponse)
def suggest_anchors_endpoint(
    req: SuggestAnchorsRequest,
) -> SuggestAnchorsResponse:
    resp, errors = suggest_anchors(req)
    if errors:
        return JSONResponse(
            status_code=422,
            content={
                "error": "anchor_suggest_failed",
                "detail": [e.model_dump() for e in errors],
            },
        )
    return resp  # type: ignore[return-value]


@app.post("/calibrate", response_model=CalibrateResponse)
def calibrate_endpoint(req: CalibrateRequest) -> CalibrateResponse:
    resp, errors = calibrate(req)
    if errors:
        return JSONResponse(
            status_code=422,
            content={
                "error": "calibration_failed",
                "detail": [e.model_dump() for e in errors],
            },
        )
    return resp  # type: ignore[return-value]


@app.post("/layout/wrap", response_model=WrapResponse)
def layout_wrap_endpoint(req: WrapRequest) -> WrapResponse:
    resp, errors = layout_wrap(req)
    if errors:
        return JSONResponse(
            status_code=422,
            content={
                "error": "layout_wrap_failed",
                "detail": [e.model_dump() for e in errors],
            },
        )
    return resp  # type: ignore[return-value]


@app.post("/edits/project", response_model=ProjectEditsResponse)
def edits_project_endpoint(req: ProjectEditsRequest) -> ProjectEditsResponse:
    resp, errors = project_edits(req)
    if errors:
        return JSONResponse(
            status_code=422,
            content={
                "error": "edit_project_failed",
                "detail": [e.model_dump() for e in errors],
            },
        )
    return resp  # type: ignore[return-value]


@app.exception_handler(RequestValidationError)
def _on_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    # pydantic v2 的错误 ctx 可能携带异常对象，需兜底为字符串才能 JSON 序列化
    return JSONResponse(
        status_code=422,
        content={
            "error": "invalid_request",
            "detail": json.loads(json.dumps(exc.errors(), default=str)),
        },
    )
