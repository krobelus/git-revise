# pylint: skip-file

from conftest import *
from gitrevise.utils import commit_range
from gitrevise.todo import CyclicFixupError, build_todos, autosquash_todos
from gitrevise.merge import conflict_id_by_file_contents
import os


def test_reuse_recorded_resolution(repo):
    bash(
        """
        git config rerere.enabled true
        git config rerere.autoUpdate true
        echo initial commit > a
        git add a
        git commit -m 'initial commit'
        echo two  > a; git commit -am 'commit two'
        echo three > a; git commit -am 'commit three'
        """
    )

    with editor_main(["-i", "HEAD~~"], input=b"y\ny\ny\ny\n") as ed:
        flip_last_two_commits(repo, ed)
        with ed.next_file() as f:
            f.replace_dedent("resolved three")
        with ed.next_file() as f:
            f.replace_dedent("resolved two")

    new_tree = repo.get_commit("HEAD").tree()

    bash("git reset --hard HEAD@{1}")

    # Now we can change the order of the two commits and reuse the recorded conflict resolution.
    with editor_main(["-i", "HEAD~~"]) as ed:
        flip_last_two_commits(repo, ed)

    assert new_tree == repo.get_commit("HEAD").tree()


def test_rerere_merge(repo):
    (repo.workdir / "a").write_bytes(b"1\n" + 8 * b"x\n" + b"10\n")
    bash(
        f"""
        git config rerere.enabled true
        git config rerere.autoUpdate true
        git add a; git commit -m 'initial commit'
        sed 1ctwo   -i a; git commit -am 'commit two'
        sed 1cthree -i a; git commit -am 'commit three'
        """
    )

    with editor_main(["-i", "HEAD~~"], input=b"y\ny\ny\ny\n") as ed:
        flip_last_two_commits(repo, ed)
        with ed.next_file() as f:
            f.replace_dedent(b"resolved1\n" + 8 * b"x\n" + b"10\n")
        with ed.next_file() as f:
            f.replace_dedent(b"resolved2\n" + 8 * b"x\n" + b"10\n")

    bash("git reset --hard HEAD@{1}")

    bash("sed 10cten -i a; git add a")
    main(["HEAD~2"])

    with editor_main(["-i", "HEAD~~"]) as ed:
        flip_last_two_commits(repo, ed)

    def hunks(diff: bytes) -> bytes:
        i = diff.index(b"@@")
        return diff[i:]

    assert (
        hunks(repo.git("show", "HEAD~"))
        == b"""\
@@ -1,4 +1,4 @@
-1
+resolved1
 x
 x
 x"""
    )

    assert (
        hunks(repo.git("show", "HEAD"))
        == b"""\
@@ -1,4 +1,4 @@
-resolved1
+resolved2
 x
 x
 x"""
    )

    leftover_index = hunks(repo.git("diff", "HEAD"))
    assert (
        leftover_index
        == b"""\
@@ -1,4 +1,4 @@
-resolved2
+three
 x
 x
 x"""
    )


def test_conflict_id_of_file_contents():
    two_conflicts = b"""\
<<<<<<<
a
=======
b
>>>>>>>

<<<<<<<
c
=======
d
>>>>>>>
"""

    assert (
        conflict_id_of_file_contents(two_conflicts)
        == "b674796e2915007a217c31fbc1c3fa0ad5b52ab2"
    )

    diff3_conflict = b"""\
<<<<<<<
a
|||||||
b
=======
c
>>>>>>>
"""
    assert (
        conflict_id_of_file_contents(diff3_conflict)
        == "c483953ab3a0876274cb965f72f7f9bcc2f4f75b"
    )

    # Conflicts are normalization by sorting the two sides.
    assert (
        conflict_id_of_file_contents(
            b"""\
<<<<<<<
a
=======
b
>>>>>>>
"""
        )
        == conflict_id_of_file_contents(
            b"""\
<<<<<<<
b
=======
a
>>>>>>>
"""
        )
    )


def flip_last_two_commits(repo: Repository, ed: Editor):
    head = repo.get_commit("HEAD")
    with ed.next_file() as f:
        assert f.startswith_dedent(
            f"""\
            pick {head.parent().oid.short()} commit two
            pick {head.oid.short()} commit three
            """
        )
        f.replace_dedent(
            f"""\
            pick {head.oid.short()} commit three
            pick {head.parent().oid.short()} commit two
            """
        )
