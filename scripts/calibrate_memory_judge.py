"""记忆判官的校准：用人工标注集验证 LLM 判官本身是否可信。

用法::

    python scripts/calibrate_memory_judge.py            # 跑一遍，打印 κ
    python scripts/calibrate_memory_judge.py --json     # 机器可读

为什么要这个脚本
----------------
`tests/test_memory_eval_judge.py` 测的是**评测代码**（解析、采样、落盘），用假模型。
它不能回答"这个判官的判断可信吗"。那需要拿人工标注对一遍，算机会校正一致率。

为什么用 κ 而不是一致率
-----------------------
大规模评测显示原始一致率会把判别能力高估 33~41 个百分点，并且对标签分布敏感：
一个永远回答"相关"的判官在一半样本为正时也能拿到 50% 一致率。κ 扣掉机会一致，
才能说明判官真的在判。

判读（Landis & Koch 分档）
--------------------------
    < 0.21  判官不可用
    0.21~0.40  轻微一致，只能看趋势
    0.41~0.60  中等一致，可参考
    0.61~0.80  高度一致，可作验收依据
    > 0.80  几乎完全一致

低于 0.41 时，`/memories quality` 的数字不该被当成结论。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

# 必须先导入 harness：它在导入期做 load_dotenv()，而 llm_config 靠它读 provider
# 配置。少了这一步，target_for() 返回 None，请求体里的 model 会是 null，
# 供应商直接 422（实测踩到：Failed to deserialize ... model: invalid type: null）。
import harness  # noqa: F401,E402
import memory_eval  # noqa: E402

LABELS = ROOT / 'tests' / 'fixtures' / 'memory_judge_labels.json'


def _note_entry(text: str) -> dict:
    """把校准集里的一句话包装成判官要的条目。"""
    return {'name': 'x', 'type': 'fact', 'description': text}


def run_recall(cases, *, complete=None) -> tuple[list[int], list[int]]:
    """逐条判"这条记忆对这条消息是否相关"，返回 (人工标签, 判官标签)。

    弃权的样本两边都不计入：判官说"判不了"时，把它算成对或错都不诚实。
    """
    human: list[int] = []
    judged: list[int] = []
    for case in cases:
        result = memory_eval.judge_recall(
            case['message'], [_note_entry(case['note'])], complete=complete)
        if result['abstain']:
            continue
        human.append(int(case['label']))
        judged.append(1 if result['relevant'] else 0)
    return human, judged


def run_quality(cases, *, complete=None) -> tuple[list[int], list[int]]:
    human: list[int] = []
    judged: list[int] = []
    for case in cases:
        result = memory_eval.judge_quality([_note_entry(case['note'])],
                                           complete=complete)
        if result['abstain']:
            continue
        human.append(int(case['label']))
        judged.append(1 if result['keep'] else 0)
    return human, judged


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', action='store_true', help='machine readable')
    parser.add_argument('--labels', default=str(LABELS))
    args = parser.parse_args()

    data = json.loads(Path(args.labels).read_text(encoding='utf-8'))
    report = {}
    for kind, runner in (('recall', run_recall), ('quality', run_quality)):
        cases = data.get(kind) or []
        try:
            human, judged = runner(cases)
        except Exception as exc:  # 网络/凭据/余额
            print(f'{kind}: 无法完成校准 —— {type(exc).__name__}: {exc}',
                  file=sys.stderr)
            print('\n判官本身没被验证之前，`/memories quality` 的数字只当参考。',
                  file=sys.stderr)
            return 2
        report[kind] = memory_eval.validate_against_labels(human, judged)
        report[kind]['skipped'] = len(cases) - len(human)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for kind, stats in report.items():
            print(f'{kind}: {stats["samples"]} 条（弃权 {stats["skipped"]}）')
            print(f'  原始一致率 {stats["raw_agreement"]} · κ {stats["kappa"]}')
            print(f'  判读：{stats["band"]} · {"可用" if stats["usable"] else "不可作为结论"}')
            print()

    # 退出码表达结论，便于接进 CI：任一判官不可用即失败。
    return 0 if all(stats['usable'] for stats in report.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
