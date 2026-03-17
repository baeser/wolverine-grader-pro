import json
import time
import urllib.request
import urllib.parse
import urllib.error


class CanvasError(Exception):
    pass


class CanvasClient:
    def __init__(self, base_url: str, api_token: str):
        base_url = base_url.strip().rstrip('/')
        if not base_url.startswith('http'):
            base_url = 'https://' + base_url
        self.base_url = base_url
        self.api_token = api_token.strip()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _auth_headers(self) -> dict:
        return {
            'Authorization': f'Bearer {self.api_token}',
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        }

    def _get(self, url: str, params: dict = None) -> tuple:
        """GET request → returns (parsed_json, link_header_str)."""
        if params:
            url = url + '?' + urllib.parse.urlencode(params, doseq=True)
        req = urllib.request.Request(url, headers=self._auth_headers())
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode('utf-8')
                link = resp.headers.get('Link', '') or ''
                return json.loads(body), link
        except urllib.error.HTTPError as e:
            body_text = e.read().decode('utf-8', errors='replace')[:300]
            if e.code == 401:
                raise CanvasError(
                    "Invalid Canvas API token. Please check your token and try again."
                )
            if e.code == 403:
                raise CanvasError(
                    "Access denied. Make sure your token has teacher-level access."
                )
            if e.code == 404:
                raise CanvasError(
                    "Canvas URL not found. Please double-check your Canvas instance URL."
                )
            raise CanvasError(f"Canvas API error {e.code}: {body_text}")
        except urllib.error.URLError as e:
            raise CanvasError(f"Could not connect to Canvas: {e.reason}")

    def _put(self, url: str, payload: dict) -> dict:
        """PUT request with JSON body → returns parsed response."""
        body = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            url, data=body, headers=self._auth_headers(), method='PUT'
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            body_text = e.read().decode('utf-8', errors='replace')[:300]
            if e.code == 401:
                raise CanvasError("Authentication failed when pushing grade.")
            if e.code == 403:
                raise CanvasError("Permission denied when pushing grade.")
            raise CanvasError(f"Failed to push grade (HTTP {e.code}): {body_text}")
        except urllib.error.URLError as e:
            raise CanvasError(f"Network error pushing grade: {e.reason}")

    def _get_paginated(self, endpoint: str, params: dict = None) -> list:
        """Fetch all pages and return combined list."""
        results = []
        params = dict(params or {})
        params.setdefault('per_page', 100)
        url = f"{self.base_url}/api/v1/{endpoint.lstrip('/')}"

        while url:
            data, link = self._get(url, params)
            if isinstance(data, list):
                results.extend(data)
            else:
                return [data]

            # Follow "next" link for pagination
            url = None
            params = {}
            for part in link.split(','):
                part = part.strip()
                if 'rel="next"' in part:
                    url = part.split(';')[0].strip().strip('<>')
                    break

        return results

    def download_file(self, url: str) -> bytes:
        """Download a Canvas-hosted file (requires auth)."""
        # Canvas file downloads may redirect; urllib follows them automatically
        req = urllib.request.Request(url, headers=self._auth_headers())
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            raise CanvasError(f"Could not download file (HTTP {e.code})")
        except urllib.error.URLError as e:
            raise CanvasError(f"Network error downloading file: {e.reason}")

    # ── Public API ────────────────────────────────────────────────────────────

    def get_courses(self) -> list:
        """Return active courses where the token owner is a teacher."""
        courses = self._get_paginated('courses', {
            'enrollment_type': 'teacher',
            'enrollment_state': 'active',
        })
        return [
            {
                'id': c['id'],
                'name': c.get('name', 'Unnamed Course'),
                'code': c.get('course_code', ''),
            }
            for c in courses
            if not c.get('access_restricted_by_date')
        ]

    def get_assignments(self, course_id: int) -> list:
        """Return all assignments for a course."""
        assignments = self._get_paginated(
            f'courses/{course_id}/assignments',
            {'order_by': 'due_at'},
        )
        return [
            {
                'id': a['id'],
                'name': a.get('name', 'Unnamed Assignment'),
                'points_possible': a.get('points_possible'),
                'submission_types': a.get('submission_types', []),
            }
            for a in assignments
        ]

    def get_submissions(self, course_id: int, assignment_id: int) -> list:
        """Return all student submissions (with user and attachment data)."""
        return self._get_paginated(
            f'courses/{course_id}/assignments/{assignment_id}/submissions',
            {
                'include[]': ['user', 'submission_comments'],
                'student_ids[]': ['all'],
            },
        )

    def post_grade(self, course_id: int, assignment_id: int,
                   user_id: int, score, comment: str) -> dict:
        """Post a numeric grade and text comment back to Canvas."""
        url = (
            f"{self.base_url}/api/v1/courses/{course_id}"
            f"/assignments/{assignment_id}/submissions/{user_id}"
        )
        return self._put(url, {
            'submission': {'posted_grade': str(score)},
            'comment': {'text_comment': comment},
        })

    # ── Quiz API ───────────────────────────────────────────────────────────

    def get_quizzes(self, course_id: int) -> list:
        """Return all quizzes for a course (Classic + New Quizzes)."""
        results = []

        # 1) Classic Quizzes — /api/v1/courses/:id/quizzes
        try:
            classic = self._get_paginated(f'courses/{course_id}/quizzes')
            for q in classic:
                results.append({
                    'id': q['id'],
                    'title': q.get('title', 'Unnamed Quiz'),
                    'points_possible': q.get('points_possible'),
                    'question_count': q.get('question_count', 0),
                    'quiz_type': 'classic',
                })
        except Exception:
            pass  # Classic Quizzes API may not be enabled

        # 2) New Quizzes — appear as assignments with is_quiz_assignment=true
        #    or submission_types=['external_tool'] with quiz_lti URL
        try:
            assignments = self._get_paginated(
                f'courses/{course_id}/assignments',
                {'order_by': 'due_at'},
            )
            classic_ids = {q['id'] for q in results}
            for a in assignments:
                # Skip if already captured as a Classic Quiz
                quiz_id = a.get('quiz_id')
                if quiz_id and quiz_id in classic_ids:
                    continue

                # Detect New Quizzes: is_quiz_assignment flag OR
                # external_tool submission with quiz LTI URL
                is_new_quiz = a.get('is_quiz_assignment', False)
                sub_types = a.get('submission_types', [])
                ext_url = a.get('external_tool_tag_attributes', {})
                if isinstance(ext_url, dict):
                    tool_url = ext_url.get('url', '')
                else:
                    tool_url = ''

                if not is_new_quiz and 'external_tool' in sub_types:
                    # Check if the external tool URL looks like a quiz LTI
                    if 'quizzes' in tool_url.lower() or 'quiz' in tool_url.lower():
                        is_new_quiz = True

                if is_new_quiz:
                    results.append({
                        'id': a['id'],
                        'title': a.get('name', 'Unnamed Quiz'),
                        'points_possible': a.get('points_possible'),
                        'question_count': None,  # Not available via assignments API
                        'quiz_type': 'new',
                    })
        except Exception:
            pass

        return results

    def get_quiz_questions(self, course_id: int, quiz_id: int) -> list:
        """Return all questions for a Classic Quiz."""
        questions = self._get_paginated(
            f'courses/{course_id}/quizzes/{quiz_id}/questions'
        )
        return [
            {
                'id': q['id'],
                'question_name': q.get('question_name', ''),
                'question_type': q.get('question_type', ''),
                'question_text': q.get('question_text', ''),
                'points_possible': q.get('points_possible', 0),
            }
            for q in questions
        ]

    def get_quiz_submissions(self, course_id: int, quiz_id: int) -> list:
        """Return all quiz submissions with user data."""
        data = self._get_paginated(
            f'courses/{course_id}/quizzes/{quiz_id}/submissions',
            {'include[]': ['user']},
        )
        return data

    def get_quiz_submission_questions(self, quiz_submission_id: int) -> list:
        """Return per-question answers for a quiz submission."""
        # This endpoint returns a dict with 'quiz_submission_questions' key
        url = f"{self.base_url}/api/v1/quiz_submissions/{quiz_submission_id}/questions"
        data, _ = self._get(url)
        if isinstance(data, dict):
            return data.get('quiz_submission_questions', [])
        return data

    # ── New Quizzes API (/api/quiz/v1) ──────────────────────────────────────

    def get_new_quiz_items(self, course_id: int, assignment_id: int) -> list:
        """Return all items (questions) for a New Quiz via the Quiz API.

        The New Quizzes API uses the assignment_id as the quiz identifier.
        Endpoint: /api/quiz/v1/courses/:course_id/quizzes/:assignment_id/items
        """
        url = f"{self.base_url}/api/quiz/v1/courses/{course_id}/quizzes/{assignment_id}/items"
        data, _ = self._get(url)
        if isinstance(data, list):
            return data
        # Some Canvas instances return paginated results differently
        return data if isinstance(data, list) else []

    def get_new_quiz_submissions(self, course_id: int, assignment_id: int) -> list:
        """Return all submissions for a New Quiz.

        Endpoint: /api/quiz/v1/courses/:course_id/quizzes/:assignment_id/submissions
        """
        url = f"{self.base_url}/api/quiz/v1/courses/{course_id}/quizzes/{assignment_id}/submissions"
        data, _ = self._get(url)
        if isinstance(data, list):
            return data
        return data if isinstance(data, list) else []

    def get_new_quiz_submission_events(self, course_id: int, assignment_id: int,
                                        submission_id: str) -> list:
        """Return answer events for a specific New Quiz submission.

        Endpoint: /api/quiz/v1/courses/:course_id/quizzes/:assignment_id/submissions/:id/events
        """
        url = (f"{self.base_url}/api/quiz/v1/courses/{course_id}"
               f"/quizzes/{assignment_id}/submissions/{submission_id}/events")
        data, _ = self._get(url)
        if isinstance(data, list):
            return data
        return data if isinstance(data, list) else []

    def post_quiz_grades(self, quiz_submission_id: int, attempt: int,
                         questions: list) -> dict:
        """Grade specific questions in a Classic Quiz submission.

        questions: list of {id: question_id, score: float, comment: str}
        """
        url = (
            f"{self.base_url}/api/v1"
            f"/quiz_submissions/{quiz_submission_id}/questions"
        )
        return self._put(url, {
            'attempt': attempt,
            'quiz_questions': [
                {
                    'id': q['id'],
                    'score': q['score'],
                    'comment': q.get('comment', ''),
                }
                for q in questions
            ],
        })
