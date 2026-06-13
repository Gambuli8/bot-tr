"""
tests/test_telegram_parse_ids.py
Cubre el hardening de _parse_id_list: solo IDs enteros, descarta comentarios
inline y basura. Regresión del bug real de producción (2026-06-13) donde un
comentario del .env entraba como id en la whitelist de admins.
"""

from notifications.telegram_listener import _parse_id_list


def test_csv_simple():
    assert _parse_id_list("123,456") == {"123", "456"}


def test_none_y_vacio():
    assert _parse_id_list(None) == set()
    assert _parse_id_list("") == set()
    assert _parse_id_list("   ") == set()


def test_descarta_comentario_inline():
    # El caso real: el .env tenía `TELEGRAM_ADMIN_CHAT_IDS=   # CSV opcional...`
    assert _parse_id_list("   # CSV opcional con permisos full") == set()
    assert _parse_id_list("123   # un comentario") == {"123"}


def test_descarta_no_enteros():
    assert _parse_id_list("123, basura, 456") == {"123", "456"}
    assert _parse_id_list("abc,def") == set()


def test_ids_negativos_de_grupo():
    # Telegram usa IDs negativos para grupos/canales.
    assert _parse_id_list("-1001234567890,123") == {"-1001234567890", "123"}


def test_acepta_lista():
    assert _parse_id_list([123, "456", "  789  "]) == {"123", "456", "789"}


def test_lista_con_basura():
    assert _parse_id_list(["123", "# comentario", ""]) == {"123"}
