from cortex.verifiers.check_feature_lookahead import main, scan_directory


def test_detects_lookahead_pattern(tmp_path):
    (tmp_path / "buggy.py").write_text(
        "def compute(df):\n"
        "    df['slot_ts'] = (df['ts'] // 300) * 300\n"
        "    return df\n",
        encoding="utf-8",
    )
    findings = scan_directory(tmp_path)
    assert len(findings) == 1
    assert findings[0]["file"].endswith("buggy.py")
    assert findings[0]["line"] == 2
    assert "slot_ts" in findings[0]["code"]


def test_clean_file_no_findings(tmp_path):
    (tmp_path / "clean.py").write_text(
        "def compute(row):\n"
        "    slot_ts = row['slot_ts']\n"
        "    return slot_ts\n",
        encoding="utf-8",
    )
    findings = scan_directory(tmp_path)
    assert findings == []


def test_ignores_commented_out_patterns(tmp_path):
    (tmp_path / "commented.py").write_text(
        "def compute():\n"
        "    # df['slot_ts'] = (df['ts'] // 300) * 300\n"
        "    return 1\n",
        encoding="utf-8",
    )
    findings = scan_directory(tmp_path)
    assert findings == []


def test_cli_exit_0_on_clean(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    ret = main(["--features-dir", str(tmp_path), "--json"])
    assert ret == 0


def test_cli_exit_1_on_dirty(tmp_path):
    (tmp_path / "bad.py").write_text(
        "df['slot_ts'] = (df['ts'] // 300) * 300\n",
        encoding="utf-8",
    )
    ret = main(["--features-dir", str(tmp_path), "--json"])
    assert ret == 1


def test_cli_missing_dir_exit_0(tmp_path):
    missing = tmp_path / "nope"
    ret = main(["--features-dir", str(missing), "--json"])
    assert ret == 0


def test_scans_subdirectories(tmp_path):
    (tmp_path / "a.py").write_text(
        "df['slot_ts'] = (df['ts'] // 300) * 300\n",
        encoding="utf-8",
    )
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.py").write_text(
        "slot_ts = epoch // 60 * 60\n",
        encoding="utf-8",
    )
    findings = scan_directory(tmp_path)
    assert len(findings) == 2


def test_honest_forward_shift_not_flagged(tmp_path):
    """The fix pattern `(ts // N) * N + N` labels bars with CLOSE time,
    making the feature available at the next slot. Must NOT be flagged."""
    (tmp_path / "fixed.py").write_text(
        "df['slot_ts'] = (df['ts'] // 300) * 300 + 300\n"
        "df['slot_ts'] = (df['epoch'] // 300) * 300 + TICK\n",
        encoding="utf-8",
    )
    findings = scan_directory(tmp_path)
    assert findings == []


def test_equality_comparison_not_flagged(tmp_path):
    """`slot_ts == ts // 300` is a comparison, not an assignment."""
    (tmp_path / "cmp.py").write_text(
        "if slot_ts == ts // 300:\n    pass\n",
        encoding="utf-8",
    )
    findings = scan_directory(tmp_path)
    assert findings == []
