"""Is a library folder external, by itself or through a folder above it (Spec 5.1a, v0.3.9 p20)."""

from reprintbuddy.bambuddy.types import LibraryFolder
from reprintbuddy.domain.printfile import is_external_folder

FOLDERS = [
    LibraryFolder(1, "Ours", None),
    LibraryFolder(2, "Inside", 1),
    LibraryFolder(3, "Mounted", None, is_external=True),
    LibraryFolder(4, "Below mounted", 3),
    LibraryFolder(5, "Deeper", 4),
    LibraryFolder(6, "Loop a", 7),
    LibraryFolder(7, "Loop b", 6),
]


def test_top_level_is_not_external():
    assert is_external_folder(FOLDERS, None) is False


def test_internal_folders():
    assert is_external_folder(FOLDERS, 1) is False
    assert is_external_folder(FOLDERS, 2) is False


def test_external_folder_itself():
    assert is_external_folder(FOLDERS, 3) is True


def test_below_an_external_folder_counts_as_external():
    assert is_external_folder(FOLDERS, 4) is True
    assert is_external_folder(FOLDERS, 5) is True


def test_a_loop_ends_and_is_not_external():
    assert is_external_folder(FOLDERS, 6) is False


def test_an_unknown_folder_is_not_external():
    """Unknown is refused elsewhere (the folder must exist); here it is only not external."""
    assert is_external_folder(FOLDERS, 99) is False
