import json
import time

from .prompt_builder import (build_system_prompt, build_essay_message,
                             build_quiz_system_prompt, build_quiz_message,
                             DEFAULT_STRICTNESS, DEFAULT_TONE)


class GradingError(Exception):
    pass


DEFAULT_MODELS = {
    'claude': 'claude-sonnet-4-20250514',
    'openai': 'gpt-4o',
    'gemini': 'gemini-2.0-flash',
}

ALLOWED_MODELS = {
    'claude': {'claude-opus-4-5', 'claude-sonnet-4-20250514', 'claude-haiku-3-5-20241022'},
    'openai': {'gpt-4o', 'gpt-4o-mini', 'o3-mini'},
    'gemini': {'gemini-2.5-pro', 'gemini-2.0-flash', 'gemini-1.5-flash'},
}

MODEL_LABELS = {
    # Claude
    'claude-opus-4-5':           'Claude Opus 4',
    'claude-sonnet-4-20250514':  'Claude Sonnet 4',
    'claude-haiku-3-5-20241022': 'Claude Haiku 3.5',
    # OpenAI
    'gpt-4o':                    'GPT-4o',
    'gpt-4o-mini':               'GPT-4o mini',
    'o3-mini':                   'o3-mini',
    # Gemini
    'gemini-2.5-pro':            'Gemini 2.5 Pro',
    'gemini-2.0-flash':          'Gemini 2.0 Flash',
    'gemini-1.5-flash':          'Gemini 1.5 Flash',
}

# Model hierarchy: cheapest → flagship (per provider)
MODEL_HIERARCHY = {
    'claude': ['claude-haiku-3-5-20241022', 'claude-sonnet-4-20250514', 'claude-opus-4-5'],
    'openai': ['gpt-4o-mini', 'gpt-4o', 'o3-mini'],
    'gemini': ['gemini-1.5-flash', 'gemini-2.0-flash', 'gemini-2.5-pro'],
}


def get_review_model(provider: str, grading_model: str) -> str:
    """Return the same model used for grading (cost-efficient default)."""
    return grading_model


def get_available_review_models(provider: str) -> list:
    """Return list of {id, label, tier} dicts for the provider, ordered cheapest→flagship.
    'tier' is 'same', 'higher', or 'highest' relative to nothing — it's absolute position."""
    hierarchy = MODEL_HIERARCHY.get(provider, [])
    return [{'id': m, 'label': MODEL_LABELS.get(m, m)} for m in hierarchy]


class GraderAI:
    def __init__(self, provider: str, api_key: str, model: str = None):
        self.provider = provider.lower()
        self.api_key = api_key

        if self.provider not in ALLOWED_MODELS:
            raise GradingError(f"Unknown provider: {provider}")

        # Validate model — fall back to default if unrecognised
        allowed = ALLOWED_MODELS[self.provider]
        default = DEFAULT_MODELS[self.provider]
        self.model = model if model in allowed else default

        if self.provider == 'claude':
            import anthropic
            self.client = anthropic.Anthropic(api_key=api_key)
        elif self.provider == 'openai':
            import openai
            self.client = openai.OpenAI(api_key=api_key)
        elif self.provider == 'gemini':
            import google.generativeai as genai
            genai.configure(api_key=api_key)
            self.client = genai  # store module reference; models are created per-call

    def grade_essay(self, rubric: str, essay_text: str, essay_name: str,
                    strictness: int = DEFAULT_STRICTNESS,
                    calibration_examples: list = None,
                    tone: str = DEFAULT_TONE,
                    custom_phrases: str = None) -> dict:
        system_prompt = build_system_prompt(rubric, strictness,
                                           calibration_examples=calibration_examples,
                                           tone=tone, custom_phrases=custom_phrases)
        user_message = build_essay_message(essay_text, essay_name)

        last_error = None
        for attempt in range(3):
            try:
                raw = self._call_api(system_prompt, user_message)
                return self._parse_response(raw)
            except GradingError:
                raise
            except Exception as e:
                last_error = e
                err_str = str(e).lower()
                if 'rate' in err_str or '429' in str(e) or 'quota' in err_str:
                    time.sleep(2 ** (attempt + 1))
                    continue
                if attempt < 2:
                    time.sleep(2)
                    continue
                break

        raise GradingError(f"Failed after 3 attempts: {last_error}")

    def grade_quiz_submission(self, questions: list, answers: list,
                              student_name: str,
                              strictness: int = DEFAULT_STRICTNESS,
                              answer_key: str = None,
                              calibration_examples: list = None,
                              tone: str = DEFAULT_TONE,
                              custom_phrases: str = None,
                              points_possible: float = None) -> dict:
        """Grade a set of quiz question-answer pairs.

        questions: list of {id, text, points, question_type}
        answers: list of {question_id, question_text, answer_text, points}
        points_possible: total points for the quiz (from Canvas assignment)
        """
        system_prompt = build_quiz_system_prompt(
            questions, strictness,
            answer_key=answer_key,
            calibration_examples=calibration_examples,
            tone=tone, custom_phrases=custom_phrases,
            points_possible=points_possible,
        )
        user_message = build_quiz_message(student_name, answers)

        last_error = None
        for attempt in range(3):
            try:
                raw = self._call_api(system_prompt, user_message)
                return self._parse_response(raw)
            except GradingError:
                raise
            except Exception as e:
                last_error = e
                err_str = str(e).lower()
                if 'rate' in err_str or '429' in str(e) or 'quota' in err_str:
                    time.sleep(2 ** (attempt + 1))
                    continue
                if attempt < 2:
                    time.sleep(2)
                    continue
                break

        raise GradingError(f"Failed after 3 attempts: {last_error}")

    def review_scores(self, system_prompt: str, user_message: str) -> dict:
        """Run a consistency review across all graded results."""
        last_error = None
        for attempt in range(3):
            try:
                raw = self._call_api(system_prompt, user_message)
                return self._parse_review_response(raw)
            except GradingError:
                raise
            except Exception as e:
                last_error = e
                err_str = str(e).lower()
                if 'rate' in err_str or '429' in str(e) or 'quota' in err_str:
                    time.sleep(2 ** (attempt + 1))
                    continue
                if attempt < 2:
                    time.sleep(2)
                    continue
                break
        raise GradingError(f"Review failed after 3 attempts: {last_error}")

    @staticmethod
    def prepare_batch_request(idx: int, system_prompt: str, user_message: str) -> dict:
        """Build a batch-ready request dict without calling the API."""
        return {
            'custom_id': str(idx),
            'system_prompt': system_prompt,
            'user_message': user_message,
        }

    def _call_api(self, system_prompt: str, user_message: str) -> str:
        if self.provider == 'claude':
            return self._call_claude(system_prompt, user_message)
        elif self.provider == 'openai':
            return self._call_openai(system_prompt, user_message)
        else:
            return self._call_gemini(system_prompt, user_message)

    def _call_claude(self, system_prompt: str, user_message: str) -> str:
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=2048,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
            )
            return response.content[0].text
        except Exception as e:
            err = str(e).lower()
            if 'authentication' in err or 'unauthorized' in err or '401' in str(e):
                raise GradingError("Invalid Anthropic API key. Please check your key and try again.")
            raise

    def _call_openai(self, system_prompt: str, user_message: str) -> str:
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                max_tokens=2048,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
            )
            return response.choices[0].message.content
        except Exception as e:
            err = str(e).lower()
            if 'authentication' in err or 'unauthorized' in err or '401' in str(e):
                raise GradingError("Invalid OpenAI API key. Please check your key and try again.")
            raise

    def _call_gemini(self, system_prompt: str, user_message: str) -> str:
        try:
            gmodel = self.client.GenerativeModel(
                model_name=self.model,
                system_instruction=system_prompt,
            )
            response = gmodel.generate_content(user_message)

            # Safety check — Gemini can block responses
            if not response.candidates:
                raise GradingError("Gemini returned no response (possibly blocked by safety filters).")

            # Check finish reason
            finish_reason = response.candidates[0].finish_reason
            # finish_reason 1 = STOP (normal), anything else may indicate an issue
            if finish_reason not in (1, 'STOP'):
                if finish_reason in (3, 'SAFETY'):
                    raise GradingError("Gemini blocked this response due to safety filters.")

            return response.text
        except GradingError:
            raise
        except Exception as e:
            err = str(e).lower()
            if 'api_key' in err or 'permission' in err or '403' in str(e) or 'invalid' in err:
                raise GradingError(
                    "Invalid Google API key. Get one free at aistudio.google.com."
                )
            raise

    def _parse_response(self, raw: str) -> dict:
        text = raw.strip()
        # Strip markdown code fences if present
        if text.startswith('```'):
            lines = text.split('\n')
            lines = lines[1:]  # remove opening fence
            if lines and lines[-1].strip() == '```':
                lines = lines[:-1]
            text = '\n'.join(lines)

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise GradingError(f"AI returned invalid JSON. Raw response:\n{raw[:500]}")

        if 'score' not in data or 'summary' not in data:
            raise GradingError(f"AI response missing required fields. Got: {list(data.keys())}")

        # Parse and validate categories — gracefully handle missing/malformed entries
        raw_cats = data.get('categories') or []
        categories = []
        for c in raw_cats:
            if not isinstance(c, dict):
                continue
            categories.append({
                'name':        str(c.get('name', 'Category')),
                'earned':      float(c.get('earned', 0)),
                'possible':    float(c.get('possible', 0)),
                'explanation': str(c.get('explanation', '')),
            })

        # Parse quiz questions array (if present — quiz grading mode)
        raw_questions = data.get('questions') or []
        parsed_questions = []
        for q in raw_questions:
            if not isinstance(q, dict):
                continue
            parsed_questions.append({
                'question_id': str(q.get('question_id', '')),
                'earned':      float(q.get('earned', 0)),
                'possible':    float(q.get('possible', 0)),
                'feedback':    str(q.get('feedback', '')),
            })

        result = {
            'score':      float(data['score']),
            'max_score':  float(data.get('max_score', 100)),
            'summary':    str(data['summary']),
            'categories': categories,
        }
        if parsed_questions:
            result['questions'] = parsed_questions
        return result

    def _parse_review_response(self, raw: str) -> dict:
        """Parse the consistency review JSON response."""
        text = raw.strip()
        if text.startswith('```'):
            lines = text.split('\n')
            lines = lines[1:]
            if lines and lines[-1].strip() == '```':
                lines = lines[:-1]
            text = '\n'.join(lines)

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise GradingError(f"Review returned invalid JSON. Raw:\n{raw[:500]}")

        overall = str(data.get('overall_assessment', ''))
        if not overall:
            raise GradingError("Review response missing 'overall_assessment'.")

        raw_flags = data.get('flags') or []
        flags = []
        for f in raw_flags:
            if not isinstance(f, dict):
                continue
            flag_type = str(f.get('flag', 'ok')).lower()
            if flag_type not in ('ok', 'low', 'high', 'inconsistent'):
                flag_type = 'ok'
            flags.append({
                'idx': int(f.get('idx', 0)),
                'filename': str(f.get('filename', '')),
                'original_score': float(f.get('original_score', 0)),
                'suggested_score': float(f.get('suggested_score', 0)),
                'flag': flag_type,
                'rationale': str(f.get('rationale', '')),
            })

        return {
            'overall_assessment': overall,
            'flags': flags,
        }
