from typing import Protocol

from grader.store import GraderStore


class CheckPublisher(Protocol):
    """Publisher must upsert idempotently by immutable publication ID."""

    def publish_result(
        self,
        *,
        attempt_id: str,
        commit_sha: str,
        passed: bool,
    ) -> None: ...


class CheckPublicationCoordinator:
    def __init__(self, *, store: GraderStore, publisher: CheckPublisher) -> None:
        self.store = store
        self.publisher = publisher

    def process_once(self) -> str:
        publication = self.store.claim_next_check_publication()
        if publication is None:
            return "idle"
        try:
            self.publisher.publish_result(
                attempt_id=publication.publication_id,
                commit_sha=publication.commit_sha,
                passed=publication.passed,
            )
        except Exception as error:
            released = self.store.release_check_publication(
                publication.publication_id,
                publication.lease_token,
                "check-publication-failure",
            )
            if not released:
                raise RuntimeError("check publication lease was lost after failure") from error
            raise RuntimeError("check publication failed closed") from error
        if not self.store.complete_check_publication(
            publication.publication_id,
            publication.lease_token,
        ):
            # Publication is immutable and idempotent; a reclaim may safely repeat it.
            raise RuntimeError("check publication lease was lost after idempotent publish")
        return "published"
