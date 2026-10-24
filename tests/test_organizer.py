from mediaserver import organizer


def test_parse_json_markdown_block():
    text = 'Here you go:\n```json\n[{"type": "movie", "name": "X"}]\n```'
    assert organizer.parse_json(text) == [{"type": "movie", "name": "X"}]


def test_parse_json_invalid():
    assert organizer.parse_json("no json here") is None


def test_build_prompt_includes_folders_and_files():
    prompt = organizer.build_prompt(
        [{"folder_name": "Show S01", "files": ["ep101.mkv"]}]
    )
    assert "Show S01" in prompt
    assert "  - ep101.mkv" in prompt


def test_list_folders(tmp_path, monkeypatch):
    (tmp_path / "Movie (2025)").mkdir()
    (tmp_path / "Movie (2025)" / "movie.mkv").touch()
    (tmp_path / "empty").mkdir()
    (tmp_path / "Show.S01E02.1080p.mkv").touch()
    monkeypatch.setattr(organizer, "TORRENTS_COMPLETE", str(tmp_path))

    folders = {c["folder_name"]: c["files"] for c in organizer.list_folders()}

    assert folders == {
        "Movie (2025)": ["movie.mkv"],
        "__single__Show": ["Show.S01E02.1080p.mkv"],
    }


def test_process_results_moves_movies_and_episodes(tmp_path, monkeypatch):
    complete, tv, movies = tmp_path / "complete", tmp_path / "tv", tmp_path / "movies"
    (complete / "Movie.2025.1080p").mkdir(parents=True)
    (complete / "Movie.2025.1080p" / "movie.mkv").touch()
    (complete / "Show S01").mkdir()
    (complete / "Show S01" / "ep102.mkv").touch()
    monkeypatch.setattr(organizer, "TORRENTS_COMPLETE", str(complete))
    monkeypatch.setattr(organizer, "TV_DIR", str(tv))
    monkeypatch.setattr(organizer, "MOVIE_DIR", str(movies))

    organizer.process_results([
        {"original_folder": "Movie.2025.1080p", "type": "movie", "name": "my movie", "episodes": []},
        {"original_folder": "Show S01", "type": "series", "name": "show",
         "episodes": [{"file": "ep102.mkv", "season": 1, "episode": 2}]},
    ])

    assert (movies / "My Movie" / "My Movie.mkv").exists()
    assert (tv / "Show" / "Season 01" / "S01E02.mkv").exists()


def test_build_candidates_ignores_last_ok_entry(tmp_path, monkeypatch):
    state = tmp_path / "model_state.json"
    state.write_text('{"_last_ok": "a:free", "a:free": {"ok": 1, "cooldown_until": 0}}')
    monkeypatch.setattr(organizer, "MODEL_STATE_FILE", str(state))
    monkeypatch.setattr(organizer, "discover_free_models", lambda: ["b:free"])
    monkeypatch.setattr(organizer, "OPENROUTER_MODEL", "")

    assert organizer.build_candidates() == ["a:free", "b:free", organizer.OPENROUTER_FALLBACK]
