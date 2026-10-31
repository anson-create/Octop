"""Reading the catalogue backwards: from a route's key to its Chinese label.

The direction matters. Everything else in this index goes 中文 -> key, because a
requirement is written in Chinese and the code is written in keys. A router is
the opposite case: CargoWare titles the sea-export section ``seaExport``, so the
only way to know a component called 海运出口 is that section is to ask what that
key renders as.
"""

from __future__ import annotations

from pathlib import Path

from octop.infra.pm.ports.i18n import I18nIndex

ZH = """
import zhCN from './zh_CN'
export default {
  ...zhCN,
  seaExport: '海运出口',
  airExport: '空运出口',
  partner: '合作伙伴',
}
"""

MENU = """
module.exports = {
  partner: '客户/海外代理',
  label1: '不重复的东西',
}
"""


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    lang = root / "src" / "lang"
    (lang / "zh_CN").mkdir(parents=True)
    (lang / "zh.js").write_text(ZH, encoding="utf-8")
    (lang / "zh_CN" / "Menu.js").write_text(MENU, encoding="utf-8")
    return root


def test_a_section_key_resolves_to_the_name_the_menu_shows(tmp_path: Path) -> None:
    index = I18nIndex.load(_repo(tmp_path))
    assert index is not None

    assert index.labels("seaExport") == ["海运出口"]
    assert index.labels("airExport") == ["空运出口"]


def test_a_key_defined_twice_returns_both_meanings(tmp_path: Path) -> None:
    """``partner`` is both 合作伙伴 and 客户/海外代理; only the caller knows which."""

    index = I18nIndex.load(_repo(tmp_path))
    assert index is not None

    assert set(index.labels("partner")) == {"合作伙伴", "客户/海外代理"}


def test_an_unknown_key_is_an_empty_answer_not_a_guess(tmp_path: Path) -> None:
    index = I18nIndex.load(_repo(tmp_path))
    assert index is not None

    assert index.labels("clearing") == []
    assert index.labels("") == []


def test_a_repo_with_no_catalogue_returns_nothing(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    assert I18nIndex.load(tmp_path / "empty") is None
