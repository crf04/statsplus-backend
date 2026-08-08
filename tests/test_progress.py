from app.services.progress import RefreshProgress, RefreshProgressStage


def test_refresh_progress_reports_canonical_milestones_and_notes():
    observed = []
    progress = RefreshProgress(lambda value, note: observed.append((value, note)))

    progress.fetch("fetching")
    progress.transform("transforming")
    progress.publish("publishing")
    progress.complete()

    assert observed == [
        (RefreshProgressStage.FETCH.value, "fetching"),
        (RefreshProgressStage.TRANSFORM.value, "transforming"),
        (RefreshProgressStage.PUBLISH.value, "publishing"),
        (RefreshProgressStage.COMPLETE.value, "Completed"),
    ]
