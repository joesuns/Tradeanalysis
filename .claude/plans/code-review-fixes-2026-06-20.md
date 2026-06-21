# 实施计划：Code Review 10 条修复

## 实施顺序

### Phase 1: connection.py 基础设施（#3 → #1 → #5 → #6 → #7 → #9）

1. **#3** 抽取 `_resolve_temp_dir(data_dir)` 辅助函数 — 先建基础设施
2. **#1** 信号处理器安全化 — `_signal_handler` 只设标记，atexit 恢复退出码
3. **#5** getsize 安全包装 `_safe_getsize()`
4. **#6** `_orphan_cleanup_done` 加锁
5. **#7** `check_connectivity` 用只读连接
6. **#9** temp_dir 路径白名单校验 `_validate_temp_dir_path()`

### Phase 2: schema.py（#2）
7. **#2** DWS 加 `(calc_date, ts_code, trade_date)` 索引

### Phase 3: cli.py + config.py（#4, #8, #10）
8. **#10** config.py 加 `DWS_PRUNE_KEEP_RUNS` env var
9. **#4** cmd_prune docstring 修正
10. **#8** cmd_check 键访问统一 .get()
11. **#10** cli.py argparse default 改用 `DWS_PRUNE_KEEP_RUNS`

### Phase 4: 验证
12. `pytest tests/ -v`
13. 更新 CLAUDE.md

## 涉及文件
- backend/db/connection.py — 6 处改动
- backend/db/schema.py — 1 处改动
- backend/cli.py — 3 处改动
- backend/config.py — 1 处改动
- CLAUDE.md — 文档更新
