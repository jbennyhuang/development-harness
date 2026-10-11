from development_harness.tracker import revision


def test_revision_ignores_scheduler_fields_and_dependency_display_metadata():
    issue = {"title": "A", "description": "B", "acceptance_criteria": "C"}
    assert revision(issue) == revision({**issue, "status": "in_progress", "assignee": "dev", "dependencies": []})
    dependency = {"id": "other", "dependency_type": "blocks", "title": "Original", "status": "open"}
    changed_display = {**dependency, "title": "Renamed", "status": "closed"}
    assert revision({**issue, "dependencies": [dependency]}) == revision({**issue, "dependencies": [changed_display]})
    assert revision(issue) != revision({**issue, "dependencies": [dependency]})
    assert revision(issue) != revision({**issue, "acceptance_criteria": "New behavior"})
