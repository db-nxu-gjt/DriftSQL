# -*- coding: utf-8 -*-
"""build_v_benign.py — E10a: V-benign 良性数据演化控制族构建 (零 API)。

动机 (评审体检 A2): 生产 DB 天天有良性 INSERT/UPDATE; D5/D6 是候选指纹信号,
必须给出"数据良性演化但不改变任何 gold 答案"的负控制。本脚本对 V 族 5 库
从原始 BIRD 库出发施加两类分布保持变换:
  - churn (列值成对交换): 仅选未被任何 gold SQL 引用的列 (FK/PK 列除外),
    成对交换精确保持列值多重集分布 → 引用它的候选 (若引用) 答案会变,
    但 gold 答案不变 → 这是筛查信号误报的来源。
  - dup (复行插入): INSERT SELECT 复制既有行 (非 PK 列), 保持值分布。
    未引用表直接安全; 引用表经逐表探针 + 全量验证保留幸存者。
累积 5 个检查点 b1..b5 (良性体量递增), 物化 dbs_vbenign/V/{db}/b{j}.sqlite。

验证门槛 (已验证良性): 全部 150 题 gold 哈希在每个 b_j 上与 t0 逐一相等
且执行成功。基线 churn 若有漏网 (正则引用分析 imperfect), 按违规归因
自动剔除 churn 列并重建, 直到 100% 通过 (确定性迭代)。

输出: dbs_vbenign/V/ + transforms/manifest_Vbenign.json
"""
from __future__ import annotations

import json
import os
import random
import re
import shutil
import sqlite3
import time
from collections import Counter, defaultdict

from build_drift_benchmark import (HERE, SEED, FAMILY_DBS, run_sql,
                                   result_fingerprint, db_path_of,
                                   list_tables, table_cols, row_count)

NB = 5                       # 良性检查点数
CHURN_FRAC = 0.10            # 每级对每列交换 10% 行 (成对)
DUP_FRACS = [0.02, 0.03, 0.05, 0.07, 0.08]   # 各级复行比例 (累计约 25%)
MAX_CHURN_COLS = 8
MAX_UNREF_DUP_TABLES = 4
MAX_REF_DUP_TABLES = 2
OUTDIR = os.path.join(HERE, "dbs_vbenign", "V")


# ---------------- 引用分析 ----------------

def question_refs(sql, tbl_cols):
    """gold SQL 引用的表/列集合 + 是否 SELECT * (大小写不敏感词边界)。"""
    tabs, cols, starred = set(), set(), bool(
        re.search(r"SELECT\s+\*|\.\*", sql, re.I))
    for tb, tcols in tbl_cols.items():
        if re.search(rf"\b{re.escape(tb)}\b", sql, re.I):
            tabs.add(tb)
            for c in tcols:
                if re.search(rf"\b{re.escape(c)}\b", sql, re.I):
                    cols.add(c)
    return tabs, cols, starred


def fk_columns(con):
    """涉及外键的 (表, 列) 全集 (父子两侧), churn 需整列避开。"""
    out = set()
    for tb in list_tables(con):
        for r in con.execute(f'PRAGMA foreign_key_list("{tb}")').fetchall():
            out.add((tb, r[3]))            # 子端 from 列
            out.add((r[2], r[4]))          # 父端 to 列
    return out


def is_churn_safe(tb, col, refs, tbl_cols, pk_of, fks):
    if (tb, col) in fks or col in pk_of.get(tb, set()):
        return False
    for tabs, cols, starred in refs:
        if tb in tabs and (col in cols or starred):
            return False
    return True


# ---------------- 良性变换 ----------------

def apply_churn(con, tb, col, frac, tag):
    """成对交换列值 (多重集精确保持); UNIQUE/CHECK 冲突对跳过。"""
    rng = random.Random(f"{SEED}:Vbenign:{tag}")
    rids = [r[0] for r in con.execute(
        f'SELECT rowid FROM "{tb}" ORDER BY rowid').fetchall()]
    n = len(rids)
    k = int(n * frac / 2)
    if k < 1:
        return 0
    rng.shuffle(rids)
    done = 0
    for i in range(k):
        a, b = rids[2 * i], rids[2 * i + 1]
        va = con.execute(f'SELECT "{col}" FROM "{tb}" WHERE rowid=?',
                         (a,)).fetchone()[0]
        vb = con.execute(f'SELECT "{col}" FROM "{tb}" WHERE rowid=?',
                         (b,)).fetchone()[0]
        try:
            con.execute(f'UPDATE "{tb}" SET "{col}"=? WHERE rowid=?', (vb, a))
            con.execute(f'UPDATE "{tb}" SET "{col}"=? WHERE rowid=?', (va, b))
            done += 1
        except sqlite3.IntegrityError:
            pass
    return done


def apply_dup(con, tb, frac, tag):
    """复行插入: 复制非 PK 列 (值分布保持); 约束冲突行跳过。"""
    rng = random.Random(f"{SEED}:Vbenign:{tag}")
    cols = table_cols(con, tb)
    pk = {r[1] for r in con.execute(
        f'PRAGMA table_info("{tb}")').fetchall() if r[5] > 0}
    copy_cols = [c for c in cols if c not in pk]
    if not copy_cols:
        return 0
    cl = ",".join(f'"{c}"' for c in copy_cols)
    rids = [r[0] for r in con.execute(
        f'SELECT rowid FROM "{tb}" ORDER BY rowid').fetchall()]
    n = len(rids)
    k = max(1, int(n * frac)) if n else 0
    if k < 1:
        return 0
    rng.shuffle(rids)
    done = 0
    for rid in rids[:k]:
        try:
            con.execute(f'INSERT INTO "{tb}" ({cl}) SELECT {cl} '
                        f'FROM "{tb}" WHERE rowid=?', (rid,))
            done += 1
        except sqlite3.IntegrityError:
            pass
    return done


def materialize_benign(db, plan, upto, outdir):
    """从原始库复制并累积应用 1..upto 的良性操作, 返回各级统计。"""
    stats = {}
    for ck in range(1, upto + 1):
        out = os.path.join(outdir, db, f"b{ck}.sqlite")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        if os.path.exists(out):
            os.remove(out)
        shutil.copyfile(db_path_of(db), out)
        con = sqlite3.connect(out)
        st = {"churn_pairs": 0, "dup_rows": 0}
        for t in range(1, ck + 1):
            for op in plan[t]:
                if op["kind"] == "churn":
                    st["churn_pairs"] += apply_churn(
                        con, op["table"], op["col"], op["frac"],
                        f"{db}:b{t}:{op['table']}:{op['col']}")
                else:
                    st["dup_rows"] += apply_dup(
                        con, op["table"], op["frac"], f"{db}:b{t}:{op['table']}")
            con.commit()
        con.close()
        stats[ck] = st
    return stats


def verify_db(db, qs, gold0, upto, outdir):
    """全部题 gold 在 b1..bupt 上重执行, 返回违规列表 [(ck,qid,kind,detail)]。"""
    v = []
    for ck in range(1, upto + 1):
        path = os.path.join(outdir, db, f"b{ck}.sqlite")
        for q in qs:
            if q["qid"] not in gold0:
                continue
            ok, err, rows = run_sql(path, q["sql"])
            if not ok:
                v.append((ck, q["qid"], "exec_fail", err))
            else:
                h, _ = result_fingerprint(rows)
                if h != gold0[q["qid"]]:
                    v.append((ck, q["qid"], "hash_change", None))
    return v


def build_plan(db, churn_cols, dup_tables):
    """确定性累积计划: 每级 churn 一轮 + dup 一轮。"""
    plan = {}
    for ck in range(1, NB + 1):
        ops = []
        for tb, col in churn_cols:
            ops.append({"kind": "churn", "table": tb, "col": col,
                        "frac": CHURN_FRAC})
        for tb in dup_tables:
            ops.append({"kind": "dup", "table": tb, "frac": DUP_FRACS[ck - 1]})
        plan[ck] = ops
    return plan


def main():
    t0 = time.time()
    with open(os.path.join(HERE, "questions", "family_V.json"),
              encoding="utf-8") as f:
        qs_all = json.load(f)
    gold0, bad0 = {}, []
    with open(os.path.join(HERE, "gold", "gold_V.jsonl"), encoding="utf-8") as f:
        for ln in f:
            r = json.loads(ln)
            g = r["checkpoints"].get("0", {})
            if g.get("exec_ok") and g.get("result_hash"):
                gold0[r["qid"]] = g["result_hash"]
            else:
                bad0.append(r["qid"])
    print(f"[vben] questions={len(qs_all)} gold0_valid={len(gold0)} "
          f"gold0_invalid={len(bad0)}", flush=True)

    manifest = {"family": "Vbenign", "kind": "benign_data_evolution",
                "base": "V 族原始 BIRD 库 (t0)",
                "gate": "全部 V 题 gold 哈希在 b1..b5 与 t0 逐一相等 (100%)",
                "ops_note": f"churn=未引用列成对值交换 {CHURN_FRAC:.0%}/级; "
                            f"dup=复行插入 累计约 {sum(DUP_FRACS):.0%}",
                "dbs": {}}
    os.makedirs(OUTDIR, exist_ok=True)

    for db in FAMILY_DBS:
        qs = [q for q in qs_all if q["db"] == db and q["qid"] in gold0]
        con = sqlite3.connect(db_path_of(db))
        tbl_cols = {tb: table_cols(con, tb) for tb in list_tables(con)}
        fks = fk_columns(con)
        pk_of = {tb: {r[1] for r in con.execute(
            f'PRAGMA table_info("{tb}")').fetchall() if r[5] > 0}
            for tb in tbl_cols}
        refs = [question_refs(q["sql"], tbl_cols) for q in qs]
        tab_n = Counter()
        for tabs, _, _ in refs:
            for tb in tabs:
                tab_n[tb] += 1

        # 候选 churn 列: 未被任何 gold 引用 (且非 PK/FK), 优先热表
        cand_cols = []
        for tb in tbl_cols:
            for c in tbl_cols[tb]:
                if is_churn_safe(tb, c, refs, tbl_cols, pk_of, fks):
                    cand_cols.append((tb, c))
        cand_cols.sort(key=lambda tc: (-tab_n.get(tc[0], 0), tc[0], tc[1]))
        churn_cols = cand_cols[:MAX_CHURN_COLS]

        # 候选 dup 表: 未引用表直接安全 (按行数取前若干) + 引用表逐表探针
        unref = sorted([tb for tb in tbl_cols if tab_n.get(tb, 0) == 0
                        and row_count(con, tb) >= 10],
                       key=lambda t: (-row_count(con, t), t))
        dup_tables = list(unref[:MAX_UNREF_DUP_TABLES])
        ref_tbs = sorted([tb for tb in tbl_cols if tab_n.get(tb, 0) > 0],
                         key=lambda t: (-tab_n[t], t))
        con.close()
        print(f"[vben:{db}] n_q={len(qs)} churn_cands={len(cand_cols)} "
              f"chosen={len(churn_cols)} unref_dup={dup_tables} "
              f"ref_probe={ref_tbs[:MAX_REF_DUP_TABLES]}", flush=True)

        # 迭代: 基线构建+验证, churn 违规归因剔除
        for it in range(12):
            plan = build_plan(db, churn_cols, dup_tables)
            materialize_benign(db, plan, NB, OUTDIR)
            viol = verify_db(db, qs, gold0, NB, OUTDIR)
            if not viol:
                break
            votes = Counter()
            for ck, qid, kind, _ in viol:
                tabs, cols, starred = refs[
                    next(i for i, q in enumerate(qs) if q["qid"] == qid)]
                hit = False
                for tc in churn_cols:
                    if tc[0] in tabs and (tc[1] in cols or starred):
                        votes[("churn",) + tc] += 1
                        hit = True
                if not hit:  # 归因不到 → 剔除该题引用表上的全部 churn 列
                    for tc in churn_cols:
                        if tc[0] in tabs:
                            votes[("churn",) + tc] += 1
            if not votes:
                churn_cols = []
            else:
                worst = max(sorted(votes), key=lambda k: votes[k])
                churn_cols = [tc for tc in churn_cols if tc != worst[1:]]
            print(f"[vben:{db}] iter{it}: violations={len(viol)} "
                  f"drop={votes and max(sorted(votes), key=lambda k: votes[k])} "
                  f"remaining_churn={len(churn_cols)}", flush=True)
        assert not viol, f"{db}: 良性验证未通过 {viol[:5]}"

        # 引用表复行探针: 逐表试探, 幸存者保留
        survivors = []
        for tb in ref_tbs[:MAX_REF_DUP_TABLES]:
            trial = dup_tables + [tb]
            plan = build_plan(db, churn_cols, trial)
            materialize_benign(db, plan, NB, OUTDIR)
            v2 = verify_db(db, qs, gold0, NB, OUTDIR)
            if v2:
                print(f"[vben:{db}] probe dup {tb}: "
                      f"REJECTED ({len(v2)} violations)", flush=True)
            else:
                dup_tables = trial
                survivors.append(tb)
                print(f"[vben:{db}] probe dup {tb}: kept", flush=True)

        # 最终构建 + 验证
        plan = build_plan(db, churn_cols, dup_tables)
        stats = materialize_benign(db, plan, NB, OUTDIR)
        viol = verify_db(db, qs, gold0, NB, OUTDIR)
        assert not viol, f"{db}: final verify failed {viol[:5]}"
        # 逐 ck 明细核验 (b5 通过则中间级也应通过, 但逐级留档)
        per_ck = {}
        for ck in range(1, NB + 1):
            v_ck = verify_db(db, qs, gold0, ck, OUTDIR)
            per_ck[str(ck)] = {"n_checked": len(qs), "violations": len(v_ck)}
        manifest["dbs"][db] = {
            "n_questions": len(qs),
            "churn_cols": [{"table": t, "col": c} for t, c in churn_cols],
            "dup_tables": dup_tables,
            "dup_ref_tables_survivors": survivors,
            "row_stats": stats,
            "verification_per_ck": per_ck,
            "verified_benign": True,
        }
        print(f"[vben:{db}] PASS: churn={len(churn_cols)}cols "
              f"dup={dup_tables} survivors={survivors} "
              f"(b5: pairs={stats[NB]['churn_pairs']} "
              f"dups={stats[NB]['dup_rows']})", flush=True)

    out = os.path.join(HERE, "transforms", "manifest_Vbenign.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    n_all = sum(m["n_questions"] for m in manifest["dbs"].values())
    print(f"[vben] manifest -> {out}", flush=True)
    print(f"[vben] GATE: {n_all}/{n_all} gold 哈希 100% 稳定 "
          f"(b1..b5 全部通过) in {(time.time()-t0)/60:.1f} min", flush=True)
    print("ALL DONE", flush=True)


if __name__ == "__main__":
    main()
