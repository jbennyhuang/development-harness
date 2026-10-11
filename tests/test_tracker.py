from development_harness.tracker import Beads, revision


def test_revision_ignores_scheduler_fields_and_dependency_display_metadata():
    issue = {"title": "A", "description": "B", "acceptance_criteria": "C"}
    assert revision(issue) == revision({**issue, "status": "in_progress", "assignee": "dev", "dependencies": []})
    dependency = {"id": "other", "dependency_type": "blocks", "title": "Original", "status": "open"}
    changed_display = {**dependency, "title": "Renamed", "status": "closed"}
    assert revision({**issue, "dependencies": [dependency]}) == revision({**issue, "dependencies": [changed_display]})
    assert revision(issue) != revision({**issue, "dependencies": [dependency]})
    assert revision(issue) != revision({**issue, "acceptance_criteria": "New behavior"})


async def test_close_uses_the_assigned_actor_without_forcing_dependencies(tmp_path):
    class ControlledBeads(Beads):
        def __init__(self):
            super().__init__(tmp_path, ["bd"])
            self.issue = {"id": "issue", "status": "in_progress", "assignee": "developer-one"}
            self.calls = []

        async def read(self, id):
            return dict(self.issue)

        async def call(self, *args):
            self.calls.append(args)
            self.issue["status"] = "closed"

    tracker = ControlledBeads()
    result = await tracker.close("issue", "Validated integration")
    assert result["status"] == "closed"
    assert tracker.calls == [("close", "issue", "--reason", "Validated integration", "--actor", "developer-one")]
    await tracker.close("issue", "Validated integration")
    assert len(tracker.calls) == 1
