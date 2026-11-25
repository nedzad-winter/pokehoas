from pathlib import Path

html = Path("pokemon_encounter/www/index.html").read_text(encoding="utf-8")


def test_league_titles():
    assert "function leagueTrainerName(" in html
    assert "leagueTrainerName(t, kind)" in html
    assert "esc(t.title)} ${esc(t.name)}" not in html


if __name__ == "__main__":
    test_league_titles()
    print("ok")
