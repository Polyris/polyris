"""
DAL for evaluate_deps Lambda (v0.79.3, ADR #75).

Lifts the raw boto3 calls in evaluate_deps/index.py behind a thin
repository class. Per CLAUDE.md "DAL repository pattern for all
DynamoDB access" — this brings evaluate_deps in line with the rule
that previously only console_api followed.

Scope of this repo: access to the pipeline-tokens table (read-only
from this Lambda — task status + pause flag lookups).
"""
from __future__ import annotations

import os
import time
import boto3
from typing import Dict, List, Optional, Any


class TokensRepo:
    """Repository for pipeline-tokens table.

    Owns:
      - Resource/client lazy init (idempotent)
      - BatchGetItem with retry on UnprocessedKeys
      - Single-item GetItem with ConsistentRead
    """

    def __init__(self, table_name: Optional[str] = None):
        self._table_name = table_name or os.environ.get(
            'TOKENS_TABLE', 'pipeline-tokens'
        )
        self._dynamodb = None  # boto3.resource('dynamodb'), lazy

    @property
    def table_name(self) -> str:
        return self._table_name

    def _resource(self):
        if self._dynamodb is None:
            self._dynamodb = boto3.resource('dynamodb')
        return self._dynamodb

    @property
    def table(self):
        return self._resource().Table(self._table_name)

    @property
    def client(self):
        return self._resource().meta.client

    # ──────────────────────────────────────────────────────────────────
    # Status lookups for dependency evaluation
    # ──────────────────────────────────────────────────────────────────

    def batch_get_statuses(
        self,
        execution_names: List[str],
        *,
        max_retries: int = 3,
    ) -> Dict[str, str]:
        """Return {execution_name: status} for the given names.

        Uses BatchGetItem with retry on UnprocessedKeys. Falls back to
        individual GetItem on batch-level errors. Missing items map to
        'not_found' in the returned dict.
        """
        results: Dict[str, str] = {name: 'not_found' for name in execution_names}
        if not execution_names:
            return results

        client = self.client
        table_name = self._table_name

        for i in range(0, len(execution_names), 100):
            batch = execution_names[i:i + 100]
            keys = [{'execution_name': {'S': name}} for name in batch]
            try:
                response = client.batch_get_item(
                    RequestItems={
                        table_name: {
                            'Keys': keys,
                            'ConsistentRead': True,
                            'ProjectionExpression': 'execution_name, #s',
                            'ExpressionAttributeNames': {'#s': 'status'},
                        }
                    }
                )
                self._absorb_batch_response(response, table_name, results)

                # Retry UnprocessedKeys with backoff
                unprocessed = response.get('UnprocessedKeys', {}).get(table_name, {})
                retry_count = 0
                while unprocessed and retry_count < max_retries:
                    retry_count += 1
                    time.sleep(0.1 * retry_count)
                    retry_response = client.batch_get_item(
                        RequestItems={table_name: unprocessed}
                    )
                    self._absorb_batch_response(retry_response, table_name, results)
                    unprocessed = retry_response.get(
                        'UnprocessedKeys', {}
                    ).get(table_name, {})
            except Exception:
                # Fallback to individual GetItem (caller logs the original error)
                for name in batch:
                    try:
                        item = self.get_status_one(name)
                        if item is not None:
                            results[name] = item
                    except Exception:
                        # Individual error — leave as 'not_found'
                        pass

        return results

    @staticmethod
    def _absorb_batch_response(
        response: Dict[str, Any],
        table_name: str,
        results: Dict[str, str],
    ) -> None:
        """Pull statuses out of a BatchGetItem response into results."""
        for item in response.get('Responses', {}).get(table_name, []):
            exec_name = item.get('execution_name', {}).get('S', '')
            status = item.get('status', {}).get('S', 'not_found')
            if exec_name:
                results[exec_name] = status

    def batch_get_skip_origins(
        self,
        execution_names: List[str],
        *,
        max_retries: int = 3,
    ) -> Dict[str, str]:
        """Return {execution_name: skip_origin} for the given names (ADR #115).

        Sparse by design: an execution_name is present in the result only if
        the `skip_origin` attribute exists on its DDB item. Absence means "no
        skip_origin recorded" — the caller's `.get(name) == 'rule'` check
        naturally treats that as not-rule-originated (today's default
        behavior for any pre-existing or non-rule-triggered skip), so no
        sentinel value or explicit default is needed here.

        Separate from batch_get_statuses (not merged into its
        ProjectionExpression) so that method's existing contract and its
        callers/tests are untouched; this is called only when the
        all_success rule actually needs to distinguish a skip's origin
        (a real failure/skip combination is comparatively rare), not on
        every evaluate_deps invocation.
        """
        results: Dict[str, str] = {}
        if not execution_names:
            return results

        client = self.client
        table_name = self._table_name

        for i in range(0, len(execution_names), 100):
            batch = execution_names[i:i + 100]
            keys = [{'execution_name': {'S': name}} for name in batch]
            try:
                response = client.batch_get_item(
                    RequestItems={
                        table_name: {
                            'Keys': keys,
                            'ConsistentRead': True,
                            'ProjectionExpression': 'execution_name, skip_origin',
                        }
                    }
                )
                self._absorb_skip_origin_response(response, table_name, results)

                unprocessed = response.get('UnprocessedKeys', {}).get(table_name, {})
                retry_count = 0
                while unprocessed and retry_count < max_retries:
                    retry_count += 1
                    time.sleep(0.1 * retry_count)
                    retry_response = client.batch_get_item(
                        RequestItems={table_name: unprocessed}
                    )
                    self._absorb_skip_origin_response(retry_response, table_name, results)
                    unprocessed = retry_response.get(
                        'UnprocessedKeys', {}
                    ).get(table_name, {})
            except Exception:
                # Best-effort: a failure here just means we fall back to
                # treating those skips as not-rule-originated (the safe,
                # non-cascading default) — never raises, unlike
                # batch_get_statuses, since skip-origin refinement is an
                # enhancement, not something the caller can't proceed without.
                pass

        return results

    @staticmethod
    def _absorb_skip_origin_response(
        response: Dict[str, Any],
        table_name: str,
        results: Dict[str, str],
    ) -> None:
        """Pull skip_origin values out of a BatchGetItem response (sparse)."""
        for item in response.get('Responses', {}).get(table_name, []):
            exec_name = item.get('execution_name', {}).get('S', '')
            skip_origin = item.get('skip_origin', {}).get('S')
            if exec_name and skip_origin:
                results[exec_name] = skip_origin

    def get_status_one(self, execution_name: str) -> Optional[str]:
        """Read a single status; return None if not found."""
        resp = self.table.get_item(
            Key={'execution_name': execution_name},
            ConsistentRead=True,
            ProjectionExpression='#s',
            ExpressionAttributeNames={'#s': 'status'},
        )
        if 'Item' not in resp:
            return None
        return resp['Item'].get('status', 'not_found')

    def is_paused(self, pipeline_execution: str) -> bool:
        """Read the pause flag stored as `_pause_{pipeline_execution}`."""
        if not pipeline_execution:
            return False
        pause_key = f"_pause_{pipeline_execution}"
        resp = self.table.get_item(
            Key={'execution_name': pause_key},
            ConsistentRead=True,
            ProjectionExpression='paused',
        )
        return bool(resp.get('Item', {}).get('paused', False))


# Module-level singleton — evaluate_deps imports `tokens_repo` and calls
# its methods. Same pattern as console_api/dal/__init__.py.
tokens_repo = TokensRepo()
