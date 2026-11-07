# -*- coding: utf-8 -*-
"""build_drift_benchmark.py — DriftSQL 漂移检测基准的不可复用数据部分构建器。

构建内容（DriftSQL/exp/data 下）:
  benchmark_spec.json                 基准规格（族/检查点/操作/协议）
  questions/family_{S,V,W1,W2}.json   分层题目子集（difficulty x db 分层，种子固定）
  transforms/manifest_{S,V,W1,W2}.json 确定性漂移操作清单（可复现任意检查点）
  dbs/{family}/{db_id}/ck{t}.sqlite   物化的漂移 DB 快照（5 个轻量库, t0=原始库不物化）
  gold/gold_{S,V,W1,W2}.jsonl         漂移感知 gold 标注（每检查点重执行 gold SQL,
                                      严格结果集: 排序后规范化行集的 sha256 + n_rows + exec 状态）

设计要点:
  - 族是平行宇宙: 每族从原始库出发独立累积演化, 族间互不污染。
  - S/V/W2 族运行在 5 个最轻量 dev 库上（物化产物可移植, 共约 23MB/库）;
    其余库的漂移版本可由 manifest + 本引擎 --materialize 确定性重建。
  - W1 为纯工作负载族: 不做任何 DB 变换, 题目难度配比随检查点时移, gold 恒为 t0 gold。
  - 所有变换自动靶向（hot 表/列由子集 SQL 引用统计决定）, 种子固定, 全程确定性。
  - W3（改写漂移）需要 LLM 生成, 留待 API 阶段, 本脚本不构建。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import random
import re
import shutil
import sqlite3
import sys
import threading
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
# BIRD dev set location: env override, else <repo>/data/bird/dev_20240627
BIRD = os.environ.get("DRIFTSQL_BIRD_DIR") or os.path.normpath(
    os.path.join(HERE, "..", "data", "bird", "dev_20240627"))
DEV_JSON = os.path.join(BIRD, "dev.json")
DBDIR = os.path.join(BIRD, "dev_databases")
SEED = 20260928
QPER = 150                     # S/V/W2 每族题目数
QW1 = 200                      # W1 池（各检查点按配比取子集）
GOLD_TIMEOUT = 15              # 秒
CKPTS = {"S": [1, 2, 3, 4, 5], "V": [1, 2, 3, 4, 5], "W2": [1, 2, 3],
         "W1": [1, 2, 3, 4]}

# 5 个最轻量库（物化目标）; 全部 11 库用于 W1
FAMILY_DBS = ["superhero", "student_club", "toxicology",
              "thrombosis_prediction", "california_schools"]

# ---------------- 基础工具 ----------------

def canon_row(row):
    return json.dumps([("" if v is None else str(v)) for v in row],
                      ensure_ascii=False, sort_keys=True)


def result_fingerprint(rows):
    rows = sorted(canon_row(r) for r in rows)
    h = hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()
    return h, len(rows)


def run_sql(db_path, sql, timeout=GOLD_TIMEOUT):
    """带超时执行, 返回 (ok, err_kind, rows)。"""
    out = {}

    def work():
        try:
            con = sqlite3.connect(db_path)
            cur = con.execute(sql)
            rows = cur.fetchall()
            con.close()
            out["ok"], out["err"], out["rows"] = True, None, rows
        except Exception as e:  # noqa: BLE001
            out["ok"] = False
            out["err"] = type(e).__name__
            out["rows"] = None
    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return False, "timeout", None
    return out.get("ok", False), out.get("err"), out.get("rows")


def list_tables(con):
    return [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'").fetchall()]


def table_cols(con, tb):
    return [r[1] for r in con.execute(f'PRAGMA table_info("{tb}")').fetchall()]


def row_count(con, tb):
    return con.execute(f'SELECT COUNT(*) FROM "{tb}"').fetchone()[0]


# ---------------- 题目子集 ----------------

def load_questions():
    with open(DEV_JSON, encoding="utf-8") as f:
        items = json.load(f)
    qs = []
    for it in items:
        qs.append({"qid": it["question_id"], "db": it["db_id"],
                   "question": it["question"], "sql": it["SQL"],
                   "difficulty": it.get("difficulty", "moderate")})
    return qs


def stratified_subset(qs, dbs, n, seed):
    """difficulty x db 分层抽样（比例配额, 不足则全取）。"""
    pool = qs if dbs is None else [q for q in qs if q["db"] in dbs]
    rng = random.Random(seed)
    by = defaultdict(list)
    for q in pool:
        by[(q["difficulty"], q["db"])].append(q)
    for v in by.values():
        rng.shuffle(v)
    strata = sorted(by.keys())
    quota = {s: max(1, round(n * len(by[s]) / len(pool))) for s in strata}
    while sum(quota.values()) > n:   # 修正取整超量
        s = max(strata, key=lambda s: quota[s])
        quota[s] -= 1
    sel = []
    for s in strata:
        sel.extend(by[s][:quota[s]])
    return sel[:n] if len(sel) > n else sel


# ---------------- 靶向与变换 ----------------

def col_usage(qs, db):
    """统计 (table, col) 在该库题目 SQL 中的引用题数。"""
    tab_n, col_n = Counter(), Counter()
    tbl_cols = {}
    con = sqlite3.connect(db_path_of(db))
    for tb in list_tables(con):
        tbl_cols[tb] = table_cols(con, tb)
    con.close()
    for q in qs:
        sql = q["sql"]
        for tb, cols in tbl_cols.items():
            if re.search(rf"\b{re.escape(tb)}\b", sql, re.I):
                tab_n[tb] += 1
                for c in cols:
                    if re.search(rf"\b{re.escape(c)}\b", sql, re.I):
                        col_n[(tb, c)] += 1
    return tab_n, col_n, tbl_cols


def db_path_of(db):
    return os.path.join(DBDIR, db, f"{db}.sqlite")


def is_date_like(con, tb, col, sample=200):
    try:
        rows = con.execute(
            f'SELECT "{col}" FROM "{tb}" WHERE "{col}" IS NOT NULL '
            f"LIMIT {sample}").fetchall()
    except Exception:  # noqa: BLE001
        return False
    if not rows:
        return False
    hits = sum(1 for (v,) in rows
               if isinstance(v, str) and re.match(r"^\d{4}-\d{2}-\d{2}", v))
    return hits / len(rows) >= 0.8


def find_date_col(con, tbl_cols, tab_n):
    cands = [(tb, c) for tb in tbl_cols for c in tbl_cols[tb]
             if is_date_like(con, tb, c)]
    if not cands:
        return None, None
    cands.sort(key=lambda tc: -tab_n.get(tc[0], 0))
    return cands[0]


def find_categorical_col(con, tbl_cols, col_n, min_q=3):
    """低基数文本列且被 >=min_q 题引用。"""
    best, best_q = (None, None), -1
    for (tb, c), n in col_n.items():
        if n < min_q:
            continue
        try:
            k = con.execute(
                f'SELECT COUNT(DISTINCT "{c}") FROM "{tb}"').fetchone()[0]
            tot = row_count(con, tb)
        except Exception:  # noqa: BLE001
            continue
        if tot >= 200 and 2 <= k <= max(20, tot * 0.02) and n > best_q:
            best, best_q = (tb, c), n
    return best


def base_manifest_ops(family, qs, db):
    """按族生成该库的全部操作（按检查点累积列表）。"""
    con = sqlite3.connect(db_path_of(db))
    tab_n, col_n, tbl_cols = col_usage(qs, db)
    hot_tb = max(tab_n, key=tab_n.get) if tab_n else None
    m = {"db": db, "n_questions": tab_n.get(hot_tb, 0), "checkpoints": {}}
    rng = random.Random(f"{SEED}:{family}:{db}")
    if hot_tb is None:
        con.close()
        return None
    pk_cols = {r[1] for r in con.execute(
        f'PRAGMA table_info("{hot_tb}")').fetchall() if r[5] > 0}
    hot_cols = sorted(((c, col_n[(hot_tb, c)]) for c in tbl_cols[hot_tb]
                       if c not in pk_cols),
                      key=lambda x: -x[1])
    if not hot_cols:
        con.close()
        return None
    hot_col = hot_cols[0][0]
    n_rows = row_count(con, hot_tb)

    if family == "S":
        rare = hot_cols[-1][0] if len(hot_cols) > 1 else hot_col
        m["checkpoints"] = {
            1: [{"op": "add_distractor", "table": hot_tb, "near": hot_col}],
            2: [{"op": "add_distractor", "table": hot_tb, "near": rare,
                 "suffix": "v2"},
                {"op": "rename_column", "table": hot_tb, "col": rare,
                 "new": f"{rare}_deprecated"}],
            3: [{"op": "rename_column", "table": hot_tb, "col": hot_col,
                 "new": f"{hot_col}_current"}],
            4: [{"op": "drop_column", "table": hot_tb, "col": hot_col}],
            5: [{"op": "rename_table", "table": hot_tb,
                 "new": f"{hot_tb}_archived"}],
        }
    elif family == "V":
        dcol = find_date_col(con, tbl_cols, tab_n)
        cat_tb, cat_col = find_categorical_col(con, tbl_cols, col_n)
        top_val = None
        if cat_col:
            top_val = con.execute(
                f'SELECT "{cat_col}" FROM "{cat_tb}" WHERE "{cat_col}" '
                f"IS NOT NULL GROUP BY 1 ORDER BY COUNT(*) DESC "
                f"LIMIT 1").fetchone()[0]
        m["targets"] = {"hot_table": hot_tb, "hot_col": hot_col,
                        "date_col": list(dcol) if dcol and dcol[0] else None,
                        "cat_table": cat_tb, "cat_col": cat_col,
                        "top_value": top_val, "n_rows": n_rows}
        cks = {1: [{"op": "inject_nulls", "table": hot_tb, "col": hot_col,
                    "frac": 0.05}],
               2: [{"op": "inject_nulls", "table": hot_tb, "col": hot_col,
                    "frac": 0.15}]}

        if dcol and dcol[0]:
            cks.setdefault(3, []).append(
                {"op": "shift_dates", "table": dcol[0], "col": dcol[1],
                 "years": 1})
        if top_val is not None:
            cks.setdefault(4, []).append(
                {"op": "delete_top_value", "table": cat_tb, "col": cat_col,
                 "value": top_val, "frac": 0.5})
            cks.setdefault(5, []).append(
                {"op": "delete_top_value", "table": cat_tb, "col": cat_col,
                 "value": top_val, "frac": 1.0})
        m["checkpoints"] = {str(k): v for k, v in cks.items()}
    elif family == "W2":
        cat_cols = [(c, col_n[(hot_tb, c)]) for c in tbl_cols[hot_tb]
                    if col_n[(hot_tb, c)] > 0]
        cat_col = cat_cols[0][0] if cat_cols else tbl_cols[hot_tb][0]
        m["targets"] = {"hot_table": hot_tb, "cat_col": cat_col}
        m["checkpoints"] = {
            1: [{"op": "insert_newcats", "table": hot_tb, "col": cat_col,
                 "n_rows": max(10, int(n_rows * 0.01))}],
            2: [{"op": "insert_newcats", "table": hot_tb, "col": cat_col,
                 "n_rows": max(20, int(n_rows * 0.03))}],
            3: [{"op": "insert_newcats", "table": hot_tb, "col": cat_col,
                 "n_rows": max(40, int(n_rows * 0.06))}],
        }
    con.close()
    m["checkpoints"] = {str(k): v for k, v in m["checkpoints"].items()}
    return m


def apply_op(con, op, rng):
    kind = op["op"]
    tb, col = op["table"], op.get("col")
    if kind == "add_distractor":
        near = op["near"]
        new = f"{near}_{op.get('suffix', 'derived')}"
        con.execute(f'ALTER TABLE "{tb}" ADD COLUMN "{new}" TEXT')
        con.execute('UPDATE "{t}" SET "{n}" = ("{o}" || "-x" || '
                    "ABS(ABS(RANDOM()) % 97))".format(t=tb, n=new, o=near))
    elif kind == "rename_column":
        con.execute(f'ALTER TABLE "{tb}" RENAME COLUMN '
                    f'"{col}" TO "{op["new"]}"')
    elif kind == "drop_column":
        cols = table_cols(con, tb)
        if col not in cols:
            return
        try:
            con.execute(f'ALTER TABLE "{tb}" DROP COLUMN "{col}"')
        except Exception:  # noqa: BLE001 — 旧 SQLite 兼容: 重建表
            keep = [c for c in cols if c != col]
            cl = ",".join(f'"{c}"' for c in keep)
            con.execute("BEGIN")
            con.execute(f'CREATE TABLE "{tb}_new" AS SELECT {cl} '
                        f'FROM "{tb}"')
            con.execute(f'DROP TABLE "{tb}"')
            con.execute(f'ALTER TABLE "{tb}_new" RENAME TO "{tb}"')
            con.execute("COMMIT")
    elif kind == "rename_table":
        con.execute(f'ALTER TABLE "{tb}" RENAME TO "{op["new"]}"')
    elif kind == "inject_nulls":
        pk = {r[1] for r in con.execute(
            f'PRAGMA table_info("{tb}")').fetchall() if r[5] > 0}
        if col in pk:
            return
        tot = row_count(con, tb)
        k = max(1, int(tot * op["frac"]))
        rids = [r[0] for r in con.execute(
            f'SELECT rowid FROM "{tb}" ORDER BY ABS(RANDOM()) '
            f"LIMIT {k}").fetchall()]
        chunk = ",".join(str(r) for r in rids)
        con.execute(f'UPDATE "{tb}" SET "{col}" = NULL '
                    f"WHERE rowid IN ({chunk})")
    elif kind == "shift_dates":
        con.execute(f'UPDATE "{tb}" SET "{col}" = '
                    f'date("{col}", \'+{op["years"]} year\') '
                    f'WHERE "{col}" IS NOT NULL AND "{col}" '
                    f"GLOB '[0-9][0-9][0-9][0-9]-*'")
    elif kind == "delete_top_value":
        tot = row_count(con, tb)
        k = max(1, int(tot * op["frac"] * 0.5))
        val = op["value"]
        con.execute("CREATE TEMP TABLE _del(rid INTEGER PRIMARY KEY)")
        con.execute(f'INSERT INTO _del(rid) SELECT rowid FROM "{tb}" '
                    f'WHERE "{op["col"]}" = ? ORDER BY ABS(RANDOM()) '
                    f"LIMIT {k}", (val,))
        con.execute(f'DELETE FROM "{tb}" WHERE rowid IN '
                    f"(SELECT rid FROM _del)")
        con.execute("DROP TABLE _del")
    elif kind == "insert_newcats":
        n = op["n_rows"]
        tot = row_count(con, tb)
        if tot == 0:
            return
        cols = table_cols(con, tb)
        pk = {r[1] for r in con.execute(
            f'PRAGMA table_info("{tb}")').fetchall() if r[5] > 0}
        copy_cols = [c for c in cols if c not in pk]
        collist = ",".join(f'"{c}"' for c in copy_cols)
        tag = f"NEWCAT_{rng.randint(1000, 9999)}"
        for i in range(n):
            row = con.execute(
                f'SELECT rowid FROM "{tb}" LIMIT 1 OFFSET ?',
                (rng.randrange(tot),)).fetchone()
            if row is None:
                return
            try:
                cur = con.execute(
                    f'INSERT INTO "{tb}" ({collist}) SELECT {collist} '
                    f'FROM "{tb}" WHERE rowid = ?', (row[0],))
                con.execute(f'UPDATE "{tb}" SET "{op["col"]}" = ? '
                            f"WHERE rowid = ?",
                            (f"{tag}_{i:04d}", cur.lastrowid))
            except sqlite3.IntegrityError:
                continue
    else:
        raise ValueError(f"unknown op {kind}")


def materialize(family, db, ckpt, manifest, out_path):
    """从原始库复制并累积应用 1..ckpt 的操作。"""
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    shutil.copyfile(db_path_of(db), out_path)
    con = sqlite3.connect(out_path)
    rng = random.Random(f"{SEED}:{family}:{db}:{ckpt}")
    for t in range(1, ckpt + 1):
        for op in manifest["checkpoints"].get(str(t), []):
            op2 = dict(op)
            op2.setdefault("table", manifest.get("targets", {})
                          .get("hot_table"))
            apply_op(con, op2, rng)
        con.commit()
    con.close()


# ---------------- W1 工作负载族 ----------------

def build_w1(qs):
    """难度配比时移: 4 个检查点的题目配比（无 DB 变换）。"""
    rng = random.Random(f"{SEED}:W1")
    pool = stratified_subset(qs, None, QW1, f"{SEED}:W1pool")
    mixes = {"t1_proportional": {"simple": 1, "moderate": 1, "challenging": 1},
             "t2_moderate_heavy": {"simple": 1, "moderate": 3, "challenging": 1},
             "t3_challenging_heavy": {"simple": 1, "moderate": 1,
                                      "challenging": 3},
             "t4_simple_heavy": {"simple": 3, "moderate": 1, "challenging": 1}}
    by_d = defaultdict(list)
    for q in pool:
        by_d[q["difficulty"]].append(q)
    for v in by_d.values():
        rng.shuffle(v)
    ckpts = {}
    used = Counter()
    for name, w in mixes.items():
        want = {d: max(1, round(QW1 * w[d] / sum(w.values())))
                for d in by_d}
        # 简单轮转复用池内题目（监控场景 = 同一批问题反复到来）
        sel = []
        tmp = {d: list(by_d[d]) for d in by_d}
        for d in by_d:
            need = want[d]
            take = [q["qid"] for q in (tmp[d] * 3)[:need]]
            sel.extend(take)
        ckpts[name] = sorted(set(sel))
    manifest = {"family": "W1", "kind": "workload_mix",
                "note": "无 DB 变换; 同一问题池按难度配比时移到来; "
                        "gold = 各题 t0 gold",
                "checkpoints": ckpts}
    return pool, manifest


# ---------------- gold 生成 ----------------

def gold_for(family, qs, manifest_by_db):
    """返回 {qid: {ckpt: gold}}; ckpt 0 为原始库。"""
    out = {}
    dbs = sorted({q["db"] for q in qs})
    maxck = max(CKPTS[family])
    for db in dbs:
        snaps = {}
        for t in range(1, maxck + 1):
            p = os.path.join(HERE, "dbs", family, db, f"ck{t}.sqlite")
            if os.path.exists(p):
                snaps[t] = p
        fam_qs = [q for q in qs if q["db"] == db]
        for q in fam_qs:
            ent = {"qid": q["qid"], "db": db, "difficulty": q["difficulty"],
                   "checkpoints": {}}
            for t in [0] + sorted(snaps):
                path = db_path_of(db) if t == 0 else snaps[t]
                ok, err, rows = run_sql(path, q["sql"])
                if ok:
                    h, n = result_fingerprint(rows)
                    ent["checkpoints"][str(t)] = {
                        "exec_ok": True, "n_rows": n, "result_hash": h}
                else:
                    ent["checkpoints"][str(t)] = {
                        "exec_ok": False, "error": err,
                        "note": "gold 未能执行 => loud 漂移锚点"}
            out[q["qid"]] = ent
    return out


# ---------------- 主流程 ----------------

def main():
    os.makedirs(HERE, exist_ok=True)
    for sub in ("questions", "transforms", "dbs", "gold"):
        os.makedirs(os.path.join(HERE, sub), exist_ok=True)
    qs = load_questions()
    all_dbs = sorted({q["db"] for q in qs})
    print(f"[data] dev questions={len(qs)} dbs={len(all_dbs)}", flush=True)
    print(f"[data] family dbs (materialized) = {FAMILY_DBS}", flush=True)

    subsets, manifests = {}, {}
    for fam in ("S", "V", "W2"):
        sub = stratified_subset(qs, FAMILY_DBS, QPER, f"{SEED}:{fam}")
        subsets[fam] = sub
        with open(os.path.join(HERE, "questions", f"family_{fam}.json"),
                  "w", encoding="utf-8") as f:
            json.dump(sub, f, ensure_ascii=False, indent=1)
        mf = {}
        for db in FAMILY_DBS:
            m = base_manifest_ops(fam, [q for q in sub if q["db"] == db], db)
            if m:
                mf[db] = m
        manifests[fam] = mf
        with open(os.path.join(HERE, "transforms", f"manifest_{fam}.json"),
                  "w", encoding="utf-8") as f:
            json.dump(mf, f, ensure_ascii=False, indent=1)
        print(f"[manifest {fam}] dbs={list(mf)} "
              f"questions={len(sub)}", flush=True)

    # 物化漂移 DB 快照
    for fam in ("S", "V", "W2"):
        for db, m in manifests[fam].items():
            for t in range(1, max(CKPTS[fam]) + 1):
                if str(t) not in m["checkpoints"]:
                    continue
                out = os.path.join(HERE, "dbs", fam, db, f"ck{t}.sqlite")
                if os.path.exists(out):
                    os.remove(out)
                materialize(fam, db, t, m, out)
            done = sorted(x for x in m["checkpoints"]
                          if os.path.exists(os.path.join(
                              HERE, "dbs", fam, db, f"ck{x}.sqlite")))
            print(f"[dbs {fam}/{db}] materialized ck={done}", flush=True)

    # W1
    w1_pool, w1_manifest = build_w1(qs)
    subsets["W1"] = w1_pool
    manifests["W1"] = w1_manifest
    with open(os.path.join(HERE, "questions", "family_W1.json"), "w",
              encoding="utf-8") as f:
        json.dump(w1_pool, f, ensure_ascii=False, indent=1)
    with open(os.path.join(HERE, "transforms", "manifest_W1.json"), "w",
              encoding="utf-8") as f:
        json.dump(w1_manifest, f, ensure_ascii=False, indent=1)
    print(f"[manifest W1] pool={len(w1_pool)}", flush=True)

    # gold 标注
    for fam in ("S", "V", "W2"):
        g = gold_for(fam, subsets[fam], manifests[fam])
        with open(os.path.join(HERE, "gold", f"gold_{fam}.jsonl"), "w",
                  encoding="utf-8") as f:
            for qid in sorted(g):
                f.write(json.dumps(g[qid], ensure_ascii=False) + "\n")
        silent = loud = stable = 0
        for ent in g.values():
            cks = ent["checkpoints"]
            base = cks.get("0", {})
            for t, e in cks.items():
                if t == "0":
                    continue
                if not e["exec_ok"]:
                    loud += 1
                elif base.get("exec_ok") and \
                        e["result_hash"] != base.get("result_hash"):
                    silent += 1
                else:
                    stable += 1
        print(f"[gold {fam}] questions={len(g)} silent_drift={silent} "
              f"loud={loud} stable={stable}", flush=True)

    # W1 gold = 各题 t0 gold
    g = gold_for("W1", subsets["W1"], manifests["W1"])
    for ent in g.values():
        ent["checkpoints"] = {"0": ent["checkpoints"]["0"]}
    with open(os.path.join(HERE, "gold", "gold_W1.jsonl"), "w",
              encoding="utf-8") as f:
        for qid in sorted(g):
            f.write(json.dumps(g[qid], ensure_ascii=False) + "\n")
    print(f"[gold W1] questions={len(g)} (t0 only)", flush=True)

    # 基准规格
    spec = {
        "name": "DriftSQL drift-detection benchmark",
        "version": "0.1",
        "base": {"dataset": "BIRD dev (dev_20240627)",
                 "questions": len(qs), "databases": all_dbs},
        "families": {
            "S_schema_evolution": {
                "kind": "db_transform", "dbs": FAMILY_DBS,
                "checkpoints": CKPTS["S"],
                "ops": ["add_distractor", "rename_column",
                        "drop_column", "rename_table"],
                "nature": "t1-t2 静默混淆; t3-t5 响亮破坏（loud 锚点）"},
            "V_value_drift": {
                "kind": "db_transform", "dbs": FAMILY_DBS,
                "checkpoints": CKPTS["V"],
                "ops": ["inject_nulls", "shift_dates", "delete_top_value"],
                "nature": "全部静默: gold 可执行但结果集漂移"},
            "W1_workload_mix": {
                "kind": "workload", "dbs": all_dbs,
                "checkpoints": 4,
                "note": "难度配比时移, gold 恒为 t0"},
            "W2_new_rows": {
                "kind": "db_transform", "dbs": FAMILY_DBS,
                "checkpoints": CKPTS["W2"],
                "ops": ["insert_newcats"],
                "nature": "聚合题 gold 静默漂移; 点查 gold 不变"},
            "W3_paraphrase": {
                "kind": "workload", "status": "pending-LLM-generation",
                "note": "同意图新改写, 需生成阶段构建"},
            "C_cold_start": {
                "kind": "transfer", "status": "reused",
                "note": "11-fold LODO, 复用 GATE-SQL 产物"},
        },
        "gold_protocol": {
            "definition": "漂移感知 gold = gold SQL 在各检查点 DB 上的执行结果",
            "fingerprint": "排序后规范化行集(canonical str rows)的 sha256",
            "labels": {"silent": "exec_ok 且 hash 与 t0 不同",
                       "loud": "gold 无法执行（改名/删列/换表名）",
                       "stable": "exec_ok 且 hash 同 t0"},
        },
        "metrics": ["detection delay (queries to alarm)",
                    "false alarm rate on stable checkpoints",
                    "detection power per family x severity",
                    "cost: tokens + re-executed queries"],
        "reproducibility": {
            "seed": SEED,
            "note": "dbs/ 只物化 5 个轻量库; 任意 (family,db,ckpt) 可由 "
                    "manifest + apply_op 确定性重建"},
    }
    with open(os.path.join(HERE, "benchmark_spec.json"), "w",
              encoding="utf-8") as f:
        json.dump(spec, f, ensure_ascii=False, indent=1)
    print("[spec] benchmark_spec.json written", flush=True)
    print("ALL DONE", flush=True)


if __name__ == "__main__":
    main()
