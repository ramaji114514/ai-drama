"""分镜表 CSV：给制片/剪辑的那一份。

它和 `--format json` 的区别不是格式，是**读者**：json 给程序，csv 给一个
双击打开就开始数镜头的人。所以三件事必须对：

1. 台词里有逗号、引号、换行时不能错列（用 csv 模块而不是拼字符串）。
2. Excel 打开不能是乱码（UTF-8 **带 BOM**）。
3. 提示词必须在表里 —— 这张表的下一站就是做图的人。
"""

from __future__ import annotations

import io
import csv as csvmod

from ai_drama.cli import SHOT_CSV_COLUMNS, shots_csv
from ai_drama.core.models import Episode, Scene, Shot


def _ep() -> Episode:
    ep = Episode(run_id="run_x", idea="一句话", title="雨夜",
                 style="冷调", target_seconds=20.0)
    sc = Scene(scene_id="sc01", title="门", location="书店", time_of_day="夜")
    sc.shots.append(Shot(
        scene_id="sc01", order=0, action="他推开门", framing="medium",
        dialogue='他说："我来了"，然后停住', duration=10.0,
        characters=["林屿"],
        image_prompt="首帧提示", last_frame="尾帧提示",
        video_prompt="运镜提示\n第二行", negative_prompt="别的手",
        video_negative="变脸"))
    sc.shots.append(Shot(scene_id="sc01", order=1, action="他坐下", duration=10.0))
    ep.scenes.append(sc)
    return ep


def test_the_table_has_one_row_per_shot_and_the_columns_we_promised():
    rows = list(csvmod.reader(io.StringIO(shots_csv(_ep()))))
    assert tuple(rows[0]) == SHOT_CSV_COLUMNS
    assert len(rows) == 3, "2 镜 + 表头"


def test_shot_numbers_start_at_one_for_humans():
    rows = list(csvmod.reader(io.StringIO(shots_csv(_ep()))))
    assert [r[1] for r in rows[1:]] == ["1", "2"]


def test_a_dialogue_with_commas_and_quotes_does_not_break_the_table():
    """台词里既有逗号又有引号 —— 手工拼逗号在这里必错列。"""
    rows = list(csvmod.reader(io.StringIO(shots_csv(_ep()))))
    dlg = rows[1][SHOT_CSV_COLUMNS.index("台词")]
    assert dlg == '他说："我来了"，然后停住'
    # 每一行列数都对，说明没被撑开
    assert all(len(r) == len(SHOT_CSV_COLUMNS) for r in rows)


def test_a_multiline_prompt_stays_inside_one_cell():
    rows = list(csvmod.reader(io.StringIO(shots_csv(_ep()))))
    video = rows[1][SHOT_CSV_COLUMNS.index("视频提示词")]
    assert video == "运镜提示\n第二行"


def test_an_empty_shot_writes_empty_cells_not_the_word_none():
    rows = list(csvmod.reader(io.StringIO(shots_csv(_ep()))))
    second = rows[2]
    for i, name in enumerate(SHOT_CSV_COLUMNS):
        if name in ("场次", "镜号", "景别", "时长秒", "画面"):
            continue
        assert second[i] == "", f"{name} 写了 {second[i]!r}"


def test_the_cli_writes_a_bom_so_excel_does_not_mojibake(tmp_path):
    """BOM 不是讲究：没有它，Excel 里的中文全是乱码，表格当场作废。"""
    import argparse
    import subprocess
    import sys

    from ai_drama.core.store import Store

    root = tmp_path
    (root / "runs").mkdir()
    store = Store(root / "runs" / "t.sqlite3")
    from ai_drama.core.models import Run

    store.create_run(Run(run_id="run_x", idea="一句话"))
    store.save_episode(_ep())
    out = root / "shots.csv"
    store._conn.close()

    code = (
        "import sys;from ai_drama.cli import main;"
        f"sys.exit(main(['shots','run_x','--format','csv','--out',r'{out}',"
        f"'--db',r'{root / 'runs' / 't.sqlite3'}','--root',r'{root}']))"
    )
    got = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, encoding="utf-8", timeout=120)
    assert got.returncode == 0, got.stdout + got.stderr
    raw = out.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "没有 BOM，Excel 会乱码"
    assert "镜号" in raw.decode("utf-8-sig")
