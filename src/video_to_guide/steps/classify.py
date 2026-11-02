"""判断一个视频是不是「操作教学」。

现实模式最容易出的问题：把吃播、探店、开箱这类「记录我在做什么」的视频，
也整理成一本操作手册，观众照着做不出来。这一步在生成步骤之前先分类，
不是教学就直接失败——宁可不产出，也不能产出假手册。
"""

from __future__ import annotations

from ..config import Settings
from ..models import Transcript, VisionNote
from ..vision.lms_client import ChatClient

PROMPT = """判断下面这个视频是不是「操作教学」。

操作教学的定义：观众看完后，能在自己的环境里照着做出一模一样的结果。必须同时满足：
1. 视频在演示「怎么做一件事」，有先后顺序；
2. 用到的材料、工具在普通家庭环境里能拿到；
3. 做完有可验证的结果（一道菜、一朵纸花、装好的装置）。

下面这些都不是操作教学（哪怕画面里有很多动作）：
- 吃播、探店、Vlog：镜头在记录「我在吃什么、我去哪玩」，不是教「你该怎么做」；
- 产品展示、开箱、B-roll：只是把东西拍好看，没有可复现的工序；
- 纯聊天、风景、游戏、影视剪辑。

如果视频没有语音（讲解文字为空或极少），只凭画面判断：
- 画面里有双手在**按顺序操作同一组物件**（拆开外壳、把零件装上去、下锅翻炒），算教学；
- 只是手持物件转动角度给镜头看、摆弄但不改变状态，不算教学。

只输出两行，不要分析过程：
结论：教学
理由：<一句话依据>

（如果不是教学，第一行写「结论：不是教学」）

视频讲解文字：
{transcript}

画面描述抽样：
{shots}
"""


def _sample_transcript(transcript: Transcript, head: int = 18, tail: int = 4) -> str:
    segs = getattr(transcript, "segments", None) or []
    if not segs:
        return "（没有语音）"
    picked = segs[:head] + (segs[-tail:] if len(segs) > head + tail else [])
    return "\n".join(f"{s.start:.0f}s {(s.text or '').strip()}" for s in picked)[:1500]


def _sample_shots(notes: list[VisionNote], n: int = 14, dense: bool = False) -> str:
    ok = [x for x in notes if getattr(x, "ok", True) and (x.interface or x.activity)]
    if not ok:
        return "（没有画面描述）"
    if dense:
        n = min(len(ok), 30)
    step = max(1, len(ok) // n)
    picked = ok[::step][:n]
    return "\n".join(
        f"- 场景：{(x.interface or '')[:40]}｜动作：{(x.activity or '')[:60]}"
        for x in picked)[:1500]


def _parse(text: str):
    """返回 True=教学 / False=非教学 / None=说不清。

    注意「不是教学」里也含「教学」两个字，必须先判「不是」。
    """
    t = (text or "").strip()
    for line in t.splitlines():
        if "结论" in line:
            if "不是" in line:
                return False
            if "教学" in line:
                return True
    if "不是教学" in t:
        return False
    if "教学" in t:
        return True
    return None


def classify_video(transcript: Transcript, notes: list[VisionNote],
                   settings: Settings) -> tuple[bool | None, str]:
    """返回 (是否教学, 理由)。None 表示模型没给出可用结论。"""
    from ..providers import sends_reasoning_effort, sends_thinking_flag
    pid, base_url, api_key, model = settings.role_client("text")
    client = ChatClient(base_url=base_url, model=model, timeout=settings.llm_timeout,
                        max_tokens=1500, temperature=0.1,
                        disable_thinking=sends_thinking_flag(pid, settings.disable_thinking),
                        reasoning_effort="none" if sends_reasoning_effort(pid) else "",
                        api_key=api_key)
    from ..steps.generate import _has_speech
    no_voice = not _has_speech(transcript)
    prompt = PROMPT.replace("{transcript}", _sample_transcript(transcript)) \
                   .replace("{shots}", _sample_shots(notes, dense=no_voice))
    if no_voice:
        prompt += "\n\n注意：这个视频没有说话声，只看画面动作判断。"
    try:
        result = client.chat(
            [{"role": "system",
              "content": "你是严谨的视频内容分类员。只输出要求的两行，不要解释过程。"},
             {"role": "user", "content": prompt}],
            max_tokens=1500)
    except Exception as e:                                    # noqa: BLE001
        return None, f"分类调用失败：{type(e).__name__}: {e}"
    verdict = _parse(result.text)
    why = ""
    for line in (result.text or "").splitlines():
        if "理由" in line:
            why = line.split("理由", 1)[-1].lstrip("：: ").strip()
            break
    return verdict, why[:160]