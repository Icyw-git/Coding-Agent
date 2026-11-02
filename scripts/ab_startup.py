"""A/B：import 期 scan_skills 的代价（同机、交替测量）。

git stash 在两次测量之间来回切，所以两次测量的机器状态尽量接近。
"""
import statistics
import subprocess
import sys

CODE = 'import sys; sys.path.insert(0,"src"); sys.path.insert(0,"."); import harness'
FILES = ['harness.py', 'src/memory.py', 'src/llm_config.py']


def run_one():
    """返回 import harness 的累计耗时（毫秒）。"""
    result = subprocess.run([sys.executable, '-X', 'importtime', '-c', CODE],
                            capture_output=True, text=True, timeout=180)
    for line in result.stderr.splitlines():
        if line.rstrip().endswith('| harness'):
            micro = float(line.split('|')[-2].strip())
            return micro / 1000.0
    return None


new_samples, old_samples = [], []
for _ in range(7):
    subprocess.run(['git', 'stash', 'push', '-q', *FILES], check=True)
    old_samples.append(run_one())
    subprocess.run(['git', 'stash', 'pop', '-q'], check=True)
    new_samples.append(run_one())

for label, samples in (('旧（import 期 scan_skills）', old_samples),
                       ('新（按需 scan）', new_samples)):
    values = [v for v in samples if v]
    print(f'{label:26} min {min(values):6.1f}ms  '
          f'p50 {statistics.median(values):6.1f}ms  n={len(values)}')

old = [v for v in old_samples if v]
new = [v for v in new_samples if v]
print(f'\n中位差 {(statistics.median(old) - statistics.median(new)):.1f}ms | '
      f'最小差 {(min(old) - min(new)):.1f}ms')
