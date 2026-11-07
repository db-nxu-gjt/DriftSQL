# -*- coding: utf-8 -*-
"""make_v4_variant.py — 从 gen_candidates.py 机械派生第二生成器变体。

变换点（其余协议不变: 10 候选/单元, temp=1.0/top_p=0.95, content logprobs）:
  1. model: deepseek-chat -> deepseek-v4-pro
  2. 请求体加 "effort": "low"（v4-pro 为 reasoning 模型, low 抑制推理开销）
  3. MAXTOK: 250 -> 800（reasoning token 占用预算, 防止 content 被截断）
  4. 输出目录: gen/ -> gen_v4/（第一生成器产物保持不动）
带断言守护: 任一替换模式计数不符即中止, 不写出文件。
"""
import ast

SRC = "gen_candidates.py"
DST = "gen_candidates_v4.py"

src = open(SRC, encoding="utf-8").read()

pats = [
    ('"model": "deepseek-chat"', '"model": "deepseek-v4-pro"', 1),
    ('"temperature": 1.0', '"effort": "low", "temperature": 1.0', 1),
    ("MAXTOK = 250", "MAXTOK = 800", 1),
]
for old, new, want in pats:
    n = src.count(old)
    assert n == want, f"pattern count {n} != {want}: {old!r}"
    src = src.replace(old, new)

n_gen = src.count('os.path.join(HERE, "gen")')
assert n_gen >= 1, "gendir pattern not found"
src = src.replace('os.path.join(HERE, "gen")',
                  'os.path.join(HERE, "gen_v4")')

header = (
    "# -*- coding: utf-8 -*-\n"
    "# gen_candidates_v4.py — 第二生成器变体: deepseek-v4-pro (effort=low)。\n"
    "# 由 make_v4_variant.py 从 gen_candidates.py 机械变换生成;\n"
    "# 输出 gen_v4/, 其余协议 (NCAND=10, temp=1.0/top_p=0.95, logprobs) 不变。\n"
)
src = header + src

ast.parse(src)
open(DST, "w", encoding="utf-8", newline="\n").write(src)
print(f"[v4] written {DST}: gendir_repl={n_gen}, "
      f"lines={src.count(chr(10)) + 1}, PARSE OK")
