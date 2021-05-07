import re
from enum import Enum
from typing import List, Optional, Tuple

from .odb import Commit, Repository, MissingObject
from .utils import run_editor, run_sequence_editor, edit_commit_message, cut_commit


class StepKind(Enum):
    PICK = "pick"
    FIXUP = "fixup"
    SQUASH = "squash"
    REWORD = "reword"
    CUT = "cut"
    INDEX = "index"

    def __str__(self) -> str:
        return str(self.value)

    @staticmethod
    def parse(instr: str) -> "StepKind":
        if "pick".startswith(instr):
            return StepKind.PICK
        if "fixup".startswith(instr):
            return StepKind.FIXUP
        if "squash".startswith(instr):
            return StepKind.SQUASH
        if "reword".startswith(instr):
            return StepKind.REWORD
        if "cut".startswith(instr):
            return StepKind.CUT
        if "index".startswith(instr):
            return StepKind.INDEX
        raise ValueError(
            f"step kind '{instr}' must be one of: pick, fixup, squash, reword, cut, or index"
        )


class Step:
    kind: StepKind
    commit: Commit
    message: Optional[bytes]

    def __init__(self, kind: StepKind, commit: Commit) -> None:
        self.kind = kind
        self.commit = commit
        self.message = None

    @staticmethod
    def parse(repo: Repository, instr: str) -> "Step":
        parsed = re.match(r"(?P<command>\S+)\s(?P<hash>\S+)", instr)
        if not parsed:
            raise ValueError(
                f"todo entry '{instr}' must follow format <keyword> <sha> <optional message>"
            )
        kind = StepKind.parse(parsed.group("command"))
        commit = repo.get_commit(parsed.group("hash"))
        return Step(kind, commit)

    def __str__(self) -> str:
        return f"{self.kind} {self.commit.oid.short()}"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Step):
            return False
        return (
            self.kind == other.kind
            and self.commit == other.commit
            and self.message == other.message
        )


def build_todos(commits: List[Commit], index: Optional[Commit]) -> List[Step]:
    steps = [Step(StepKind.PICK, commit) for commit in commits]
    if index:
        steps.append(Step(StepKind.INDEX, index))
    return steps


def validate_todos(old: List[Step], new: List[Step]) -> None:
    """Raise an exception if the new todo list is malformed compared to the
    original todo list"""
    old_set = set(o.commit.oid for o in old)
    new_set = set(n.commit.oid for n in new)

    assert len(old_set) == len(old), "Unexpected duplicate original commit!"
    if len(new_set) != len(new):
        # XXX(nika): Perhaps print which commits are duplicates?
        raise ValueError("Unexpected duplicate commit found in todos")

    if new_set - old_set:
        # XXX(nika): Perhaps print which commits were found?
        raise ValueError("Unexpected commits not referenced in original TODO list")

    if old_set - new_set:
        # XXX(nika): Perhaps print which commits were omitted?
        raise ValueError("Unexpected commits missing from TODO list")

    saw_index = False
    for step in new:
        if step.kind == StepKind.INDEX:
            saw_index = True
        elif saw_index:
            raise ValueError("'index' actions follow all non-index todo items")


def add_autosquash_step(step: Step, picks: List[List[Step]]) -> None:
    needle = summary = step.commit.summary()
    while needle.startswith("fixup! ") or needle.startswith("squash! "):
        needle = needle.split(maxsplit=1)[1]

    if needle != summary:
        if summary.startswith("fixup!"):
            new_step = Step(StepKind.FIXUP, step.commit)
        else:
            assert summary.startswith("squash!")
            new_step = Step(StepKind.SQUASH, step.commit)

        for seq in picks:
            if seq[0].commit.summary().startswith(needle):
                seq.append(new_step)
                return

        try:
            target = step.commit.repo.get_commit(needle)
            for seq in picks:
                if any(s.commit == target for s in seq):
                    seq.append(new_step)
                    return
        except (ValueError, MissingObject):
            pass

    picks.append([step])


def autosquash_todos(todos: List[Step]) -> List[Step]:
    picks: List[List[Step]] = []
    for step in todos:
        add_autosquash_step(step, picks)
    return [s for p in picks for s in p]


def edit_todos_msgedit(repo: Repository, todos: List[Step]) -> List[Step]:
    todos_text = b""
    for step in todos:
        todos_text += f"++ {step}\n".encode()
        todos_text += step.commit.message + b"\n"

    # Invoke the editors to parse commit messages.
    response = run_editor(
        repo,
        "git-revise-todo",
        todos_text,
        comments=f"""\
        Interactive Revise Todos({len(todos)} commands)

        Commands:
         p, pick <commit> = use commit
         r, reword <commit> = use commit, but edit the commit message
         s, squash <commit> = use commit, but meld into previous commit
         f, fixup <commit> = like squash, but discard this commit's message
         c, cut <commit> = interactively split commit into two smaller commits
         i, index <commit> = leave commit changes staged, but uncommitted

        Each command block is prefixed by a '++' marker, followed by the command to
        run, the commit hash and after a newline the complete commit message until
        the next '++' marker or the end of the file.

        Commit messages will be reworded to match the provided message before the
        command is performed.

        These blocks are executed from top to bottom. They can be re-ordered and
        their commands can be changed, however the number of blocks must remain
        identical. If present, index blocks must be at the bottom of the list,
        i.e. they can not be followed by non-index blocks.


        If you remove everything, the revising process will be aborted.
        """,
    )

    # Parse the response back into a list of steps
    result = []
    for full in re.split(br"^\+\+ ", response, flags=re.M)[1:]:
        cmd, message = full.split(b"\n", maxsplit=1)

        step = Step.parse(repo, cmd.decode(errors="replace").strip())
        step.message = message.strip() + b"\n"
        result.append(step)

    validate_todos(todos, result)

    return result


def edit_todos(
    repo: Repository, todos: List[Step], msgedit: bool = False
) -> List[Step]:
    if msgedit:
        return edit_todos_msgedit(repo, todos)

    todos_text = b""
    for step in todos:
        todos_text += f"{step} {step.commit.summary()}\n".encode()

    response = run_sequence_editor(
        repo,
        "git-revise-todo",
        todos_text,
        comments=f"""\
        Interactive Revise Todos ({len(todos)} commands)

        Commands:
         p, pick <commit> = use commit
         r, reword <commit> = use commit, but edit the commit message
         s, squash <commit> = use commit, but meld into previous commit
         f, fixup <commit> = like squash, but discard this commit's log message
         c, cut <commit> = interactively split commit into two smaller commits
         i, index <commit> = leave commit changes staged, but uncommitted

        These lines are executed from top to bottom. They can be re-ordered and
        their commands can be changed, however the number of lines must remain
        identical. If present, index lines must be at the bottom of the list,
        i.e. they can not be followed by non-index lines.

        If you remove everything, the revising process will be aborted.
        """,
    )

    # Parse the response back into a list of steps
    result = []
    for line in response.splitlines():
        if line.isspace():
            continue
        step = Step.parse(repo, line.decode(errors="replace").strip())
        result.append(step)

    validate_todos(todos, result)

    return result


def is_fixup(todo: Step) -> bool:
    return todo.kind in (StepKind.FIXUP, StepKind.SQUASH)


def squash_message_template(
    target_message: bytes, fixups: List[Tuple[StepKind, bytes]]
) -> bytes:
    fused = (
        b"# This is a combination of %d commits.\n" % (len(fixups) + 1)
        + b"# This is the 1st commit message:\n"
        + b"\n"
        + target_message
    )

    for i, (kind, message) in enumerate(fixups):
        fused += b"\n"
        if kind == StepKind.FIXUP:
            fused += (
                b"# The commit message #%d will be skipped:\n" % (i + 2)
                + b"\n"
                + b"".join(b"# " + line for line in message.splitlines(keepends=True))
            )
        else:
            assert kind == StepKind.SQUASH
            fused += b"# This is the commit message #%d:\n\n%s" % (i + 2, message)

    return fused


def apply_todos(current: Commit, todos: List[Step], reauthor: bool = False) -> Commit:
    fixups: List[Tuple[StepKind, bytes]] = []

    for i, step in enumerate(todos):
        rebased = step.commit.rebase(current).update(message=step.message)
        if step.kind == StepKind.PICK:
            current = rebased
        elif is_fixup(step):
            if not fixups:
                fixup_target_message = current.message
            fixups.append((step.kind, rebased.message))
            current = current.update(tree=rebased.tree())
            is_last_fixup = i + 1 == len(todos) or not is_fixup(todos[i + 1])
            if is_last_fixup and any(
                kind == StepKind.SQUASH for kind, message in fixups
            ):
                current = current.update(
                    message=squash_message_template(fixup_target_message, fixups)
                )
                current = edit_commit_message(current)
                fixups.clear()
        elif step.kind == StepKind.REWORD:
            current = edit_commit_message(rebased)
        elif step.kind == StepKind.CUT:
            current = cut_commit(rebased)
        elif step.kind == StepKind.INDEX:
            break
        else:
            raise ValueError(f"Unknown StepKind value: {step.kind}")

        if reauthor:
            current = current.update(author=current.repo.default_author)

        print(f"{step.kind.value:6} {current.oid.short()}  {current.summary()}")

    return current
