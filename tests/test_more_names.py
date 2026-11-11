import sys
sys.path.insert(0, "pokemon_encounter")

from app import i18n


def test_run_summary():
    assert i18n.t("fr", "auto_rules.run_summary", handled=1, total=6) == "Rencontres traitées par les règles : 1 sur 6."
    assert i18n.t("en", "auto_rules.run_summary", handled=1, total=6) == "Rules handled 1 of 6 encounters."


if __name__ == "__main__":
    test_run_summary()
    print("ok")
