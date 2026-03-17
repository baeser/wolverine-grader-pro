"""
Batch API wrapper for Anthropic and OpenAI.
Provides 50% token discount via async batch processing.
"""
import io
import json
import time


class BatchError(Exception):
    pass


class BatchGrader:
    """Submit and poll batch grading jobs via provider Batch APIs."""

    def __init__(self, provider: str, api_key: str, model: str):
        self.provider = provider.lower()
        self.api_key = api_key
        self.model = model

        if self.provider == 'claude':
            import anthropic
            self.client = anthropic.Anthropic(api_key=api_key)
        elif self.provider == 'openai':
            import openai
            self.client = openai.OpenAI(api_key=api_key)
        else:
            raise BatchError(
                f"Batch grading is not available for {provider}. "
                "Use 'Grade Now' instead, or switch to Claude or OpenAI."
            )

    def submit_batch(self, requests: list) -> str:
        """Submit a batch of grading requests. Returns batch_id.

        requests: list of {custom_id: str, system_prompt: str, user_message: str}
        """
        if self.provider == 'claude':
            return self._submit_anthropic(requests)
        elif self.provider == 'openai':
            return self._submit_openai(requests)
        raise BatchError(f"Unsupported provider for batch: {self.provider}")

    def poll_batch(self, batch_id: str) -> dict:
        """Poll batch status. Returns {status, results?, error?}.

        status: 'pending' | 'in_progress' | 'completed' | 'failed' | 'expired'
        results: list of {custom_id: str, raw_text: str} when completed
        """
        if self.provider == 'claude':
            return self._poll_anthropic(batch_id)
        elif self.provider == 'openai':
            return self._poll_openai(batch_id)
        raise BatchError(f"Unsupported provider for batch: {self.provider}")

    # ── Anthropic ────────────────────────────────────────────────

    def _submit_anthropic(self, requests: list) -> str:
        """Submit batch via Anthropic Messages Batch API."""
        batch_requests = []
        for req in requests:
            batch_requests.append({
                'custom_id': req['custom_id'],
                'params': {
                    'model': self.model,
                    'max_tokens': 2048,
                    'system': req['system_prompt'],
                    'messages': [
                        {'role': 'user', 'content': req['user_message']},
                    ],
                },
            })

        try:
            batch = self.client.batches.create(requests=batch_requests)
            return batch.id
        except Exception as e:
            err = str(e).lower()
            if 'authentication' in err or 'unauthorized' in err or '401' in str(e):
                raise BatchError("Invalid Anthropic API key.")
            raise BatchError(f"Failed to submit Anthropic batch: {e}")

    def _poll_anthropic(self, batch_id: str) -> dict:
        """Poll Anthropic batch status."""
        try:
            batch = self.client.batches.retrieve(batch_id)
        except Exception as e:
            raise BatchError(f"Failed to poll Anthropic batch: {e}")

        status = batch.processing_status  # 'in_progress', 'ended'
        if status == 'ended':
            # Fetch results
            results = []
            try:
                for result in self.client.batches.results(batch_id):
                    custom_id = result.custom_id
                    if result.result.type == 'succeeded':
                        raw_text = result.result.message.content[0].text
                        results.append({'custom_id': custom_id, 'raw_text': raw_text})
                    else:
                        error_msg = getattr(result.result, 'error', {})
                        results.append({
                            'custom_id': custom_id,
                            'raw_text': None,
                            'error': str(error_msg),
                        })
            except Exception as e:
                raise BatchError(f"Failed to retrieve Anthropic batch results: {e}")

            return {'status': 'completed', 'results': results}

        return {'status': 'in_progress'}

    # ── OpenAI ───────────────────────────────────────────────────

    def _submit_openai(self, requests: list) -> str:
        """Submit batch via OpenAI Batch API."""
        # Build JSONL content
        jsonl_lines = []
        for req in requests:
            jsonl_lines.append(json.dumps({
                'custom_id': req['custom_id'],
                'method': 'POST',
                'url': '/v1/chat/completions',
                'body': {
                    'model': self.model,
                    'max_tokens': 2048,
                    'messages': [
                        {'role': 'system', 'content': req['system_prompt']},
                        {'role': 'user', 'content': req['user_message']},
                    ],
                },
            }))
        jsonl_content = '\n'.join(jsonl_lines)

        try:
            # Upload the JSONL file
            file_obj = self.client.files.create(
                file=io.BytesIO(jsonl_content.encode('utf-8')),
                purpose='batch',
            )

            # Create the batch
            batch = self.client.batches.create(
                input_file_id=file_obj.id,
                endpoint='/v1/chat/completions',
                completion_window='24h',
            )
            return batch.id
        except Exception as e:
            err = str(e).lower()
            if 'authentication' in err or 'unauthorized' in err or '401' in str(e):
                raise BatchError("Invalid OpenAI API key.")
            raise BatchError(f"Failed to submit OpenAI batch: {e}")

    def _poll_openai(self, batch_id: str) -> dict:
        """Poll OpenAI batch status."""
        try:
            batch = self.client.batches.retrieve(batch_id)
        except Exception as e:
            raise BatchError(f"Failed to poll OpenAI batch: {e}")

        status = batch.status
        # OpenAI statuses: validating, in_progress, finalizing, completed, failed, expired, cancelled
        if status == 'completed':
            output_file_id = batch.output_file_id
            if not output_file_id:
                raise BatchError("OpenAI batch completed but no output file.")

            try:
                content = self.client.files.content(output_file_id)
                text = content.text
            except Exception as e:
                raise BatchError(f"Failed to download OpenAI batch results: {e}")

            results = []
            for line in text.strip().split('\n'):
                if not line.strip():
                    continue
                entry = json.loads(line)
                custom_id = entry.get('custom_id', '')
                response = entry.get('response', {})
                if response.get('status_code') == 200:
                    body = response.get('body', {})
                    choices = body.get('choices', [])
                    raw_text = choices[0]['message']['content'] if choices else None
                    results.append({'custom_id': custom_id, 'raw_text': raw_text})
                else:
                    results.append({
                        'custom_id': custom_id,
                        'raw_text': None,
                        'error': str(response.get('error', 'Unknown error')),
                    })

            return {'status': 'completed', 'results': results}

        elif status in ('failed', 'expired', 'cancelled'):
            errors = []
            if batch.errors and batch.errors.data:
                errors = [e.message for e in batch.errors.data[:3]]
            return {
                'status': 'failed',
                'error': '; '.join(errors) if errors else f'Batch {status}',
            }

        # validating, in_progress, finalizing
        return {'status': 'in_progress'}
