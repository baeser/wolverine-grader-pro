STRICTNESS_LEVELS = {
    2: {
        "label": "Effort Counts!",
        "emoji": "🌟",
        "instructions": (
            "Grade with significant generosity. Prioritize rewarding genuine effort and "
            "participation over technical perfection. Give the benefit of the doubt on "
            "ambiguous or underdeveloped work. Minor grammar errors, weak transitions, "
            "and slightly underdeveloped arguments should NOT cost points. A student who "
            "made a sincere attempt should retain the vast majority of possible points. "
            "Reserve point deductions only for clearly missing or fundamentally incorrect "
            "rubric elements."
        ),
    },
    4: {
        "label": "Standard Classroom Mode",
        "emoji": "📚",
        "instructions": (
            "Grade with average classroom fairness. Apply the rubric consistently but "
            "with reasonable generosity. Minor grammar or stylistic issues can be noted "
            "in feedback but should not significantly impact the score. Deduct points "
            "for clearly missing elements or weak fulfillment of rubric criteria, but "
            "avoid piling on for small imperfections. Most competent work should earn "
            "a B range or higher."
        ),
    },
    6: {
        "label": "Red Pen Activated",
        "emoji": "🖊️",
        "instructions": (
            "Grade with above-average rigor. Apply the rubric firmly. Identify and "
            "penalize all noticeable weaknesses — weak arguments, missing evidence, "
            "grammar patterns, underdeveloped analysis, and citation issues all cost "
            "real points. Do not round up or be lenient on partially-met criteria. "
            "Reserve the A range for work that clearly and thoroughly fulfills every "
            "rubric element. Average work should land in the C-to-B range."
        ),
    },
    8: {
        "label": "Grammar Detective",
        "emoji": "🔍",
        "instructions": (
            "Grade with high academic rigor. Every rubric criterion must be substantively "
            "and completely met to earn full credit for that category. Grammar, mechanics, "
            "citation format, logical structure, and argument quality are all examined "
            "closely. Partial fulfillment earns partial credit at best. An A (90%+) "
            "requires strong, polished, nearly-flawless execution across the board. "
            "Most student work should land in the C-to-B range. Flag every noticeable "
            "error in the feedback."
        ),
    },
    10: {
        "label": "College Professor Who Hates You",
        "emoji": "💀",
        "instructions": (
            "Grade with maximum rigor. The rubric is law. Apply it with zero lenience. "
            "Every flaw — logical gaps, missing evidence, stylistic weakness, grammar "
            "errors, incorrect citation format, weak thesis — costs points. Partial "
            "credit is minimal. An A range (90%+) is reserved exclusively for "
            "publication-quality, truly exceptional work. A solid B (80-85%) should "
            "represent genuinely good work. Most student work should fall in the C range "
            "or below. Do not soften criticism — be direct and specific about every "
            "deficiency."
        ),
    },
}

DEFAULT_STRICTNESS = 4


def get_strictness_info(level: int) -> dict:
    """Return the strictness dict for a given level, snapping to nearest valid stop."""
    valid = sorted(STRICTNESS_LEVELS.keys())
    closest = min(valid, key=lambda x: abs(x - level))
    return {**STRICTNESS_LEVELS[closest], "level": closest}


def build_system_prompt(rubric: str, strictness: int = DEFAULT_STRICTNESS,
                        calibration_examples: list = None) -> str:
    info = get_strictness_info(strictness)

    # Build the calibration block if teacher provided exemplars
    calibration_block = ""
    if calibration_examples:
        parts = []
        parts.append(
            "\n\nCALIBRATION — TEACHER-GRADED EXAMPLES\n"
            "The teacher has graded the following example essays to show you EXACTLY how they "
            "apply this rubric. You MUST match this grading style, tone, score range, and "
            "feedback detail level as closely as possible when grading all remaining essays.\n"
        )
        for i, ex in enumerate(calibration_examples, 1):
            label = ex.get('label', f'Example {i}')
            parts.append(f"--- EXAMPLE {i}: {label} ---")
            parts.append(f"ESSAY TEXT:\n{ex['text']}\n")
            parts.append(f"TEACHER'S SCORE: {ex['score']}/{ex['max_score']}")
            parts.append(f"TEACHER'S FEEDBACK:\n{ex['feedback']}\n")
        parts.append(
            "--- END OF CALIBRATION EXAMPLES ---\n"
            "Use these examples as your ground truth. When in doubt about how strict to be "
            "or what tone to use, refer back to how the teacher scored and commented on the "
            "examples above. Your scores and feedback style must be consistent with the "
            "teacher's demonstrated preferences."
        )
        calibration_block = "\n".join(parts)

    return f"""You are an experienced academic grading assistant. You will grade student essays according to the rubric provided below.

RUBRIC:
{rubric}

GRADING MODE: {info['emoji']} {info['label']} (Level {info['level']}/10)
{info['instructions']}
{calibration_block}

INSTRUCTIONS:
1. Read the essay carefully and thoroughly.
2. Evaluate it against EACH criterion in the rubric, applying the grading mode above.
3. For every rubric criterion, determine: the criterion name, points earned, points possible, and a 1-2 sentence explanation that references SPECIFIC content from the student's essay. Do NOT give generic feedback like "your argument could be stronger." Instead, cite a specific claim, example, or passage the student wrote and explain what worked or what was missing. For example: "While you mention the Battle of Midway, you don't fully explain what made it a turning point" or "Your comparison of photosynthesis to a factory assembly line was a strong analogy that showed real understanding."
4. Sum the category scores to get the total score. Determine max_score from the rubric.
5. Write a 3-5 sentence overall feedback paragraph addressed directly to the student (use "you/your"). Start with something specific they did well (reference their actual writing), then explain where points were lost by pointing to specific examples, claims, or sections in their essay that fell short. Every piece of feedback must connect to something the student actually wrote.
6. Your tone and strictness MUST reflect the grading mode specified above.{' If calibration examples were provided, your scoring and feedback tone MUST be consistent with the teachers demonstrated style.' if calibration_examples else ''}

You MUST respond in EXACTLY this JSON format and nothing else:
{{
  "score": <number>,
  "max_score": <number>,
  "summary": "<overall feedback paragraph>",
  "categories": [
    {{"name": "<criterion name>", "earned": <number>, "possible": <number>, "explanation": "<1-2 sentence explanation>"}},
    ...one entry per rubric criterion...
  ]
}}

If the rubric does not define clear named categories, return an empty array for "categories".
Do not include any text outside the JSON object."""


def build_essay_message(essay_text: str, essay_name: str) -> str:
    return f"""STUDENT ESSAY (filename: {essay_name}):

{essay_text}"""


def build_quiz_system_prompt(questions: list, strictness: int = DEFAULT_STRICTNESS,
                             answer_key: str = None,
                             calibration_examples: list = None) -> str:
    """Build system prompt for grading quiz essay/short-answer questions.

    questions: list of {id, text, points, question_type}
    answer_key: optional teacher-provided expected answers or grading criteria
    """
    info = get_strictness_info(strictness)

    # Build the question reference block
    q_lines = []
    for i, q in enumerate(questions, 1):
        q_lines.append(f"  Q{i} (ID {q['id']}, {q['points']} pts, {q['question_type']}): {q['text']}")
    questions_block = "\n".join(q_lines)

    # Build optional answer key block
    answer_key_block = ""
    if answer_key and answer_key.strip():
        answer_key_block = f"""

ANSWER KEY / GRADING CRITERIA (provided by teacher):
{answer_key.strip()}

Use this answer key as your primary reference for evaluating correctness. Award full credit when the student's answer substantially matches the expected answer. Award partial credit when the student demonstrates partial understanding. Award no credit only when the answer is clearly wrong or missing."""

    # Build calibration block (reuse same pattern as essay grading)
    calibration_block = ""
    if calibration_examples:
        parts = []
        parts.append(
            "\n\nCALIBRATION — TEACHER-GRADED EXAMPLES\n"
            "The teacher has graded the following example submissions to show you EXACTLY how they "
            "apply their standards. You MUST match this grading style, tone, score range, and "
            "feedback detail level as closely as possible when grading all remaining submissions.\n"
        )
        for i, ex in enumerate(calibration_examples, 1):
            label = ex.get('label', f'Example {i}')
            parts.append(f"--- EXAMPLE {i}: {label} ---")
            parts.append(f"SUBMISSION:\n{ex['text']}\n")
            parts.append(f"TEACHER'S SCORE: {ex['score']}/{ex['max_score']}")
            parts.append(f"TEACHER'S FEEDBACK:\n{ex['feedback']}\n")
        parts.append(
            "--- END OF CALIBRATION EXAMPLES ---\n"
            "Use these examples as your ground truth. Your scores and feedback style must be "
            "consistent with the teacher's demonstrated preferences."
        )
        calibration_block = "\n".join(parts)

    # Build question ID list for JSON format
    q_id_examples = []
    for q in questions[:2]:
        q_id_examples.append(
            f'    {{"question_id": "{q["id"]}", "earned": <number>, '
            f'"possible": {q["points"]}, "feedback": "<specific feedback>"}}'
        )
    q_id_rest = ',\n    ...one entry per question...' if len(questions) > 2 else ''

    return f"""You are an experienced academic grading assistant. You will grade student answers to quiz questions.

QUIZ QUESTIONS:
{questions_block}
{answer_key_block}

GRADING MODE: {info['emoji']} {info['label']} (Level {info['level']}/10)
{info['instructions']}
{calibration_block}

INSTRUCTIONS:
1. Read each question and the student's answer carefully.
2. Evaluate each answer independently against the question asked, applying the grading mode above.
3. For each question, determine: points earned (0 to the question's max points) and write 1-2 sentences of specific feedback. Reference what the student actually wrote — do NOT give generic feedback. For example: "You correctly identified photosynthesis as the process but missed the role of chlorophyll" rather than "Your answer could be more complete."
4. Sum the per-question scores to get the total score.
5. Write a 2-4 sentence overall summary addressed directly to the student (use "you/your"). Mention what they did well and where they lost points, referencing specific answers.
6. Your tone and strictness MUST reflect the grading mode specified above.{' If calibration examples were provided, your scoring and feedback tone MUST be consistent with the teachers demonstrated style.' if calibration_examples else ''}

You MUST respond in EXACTLY this JSON format and nothing else:
{{
  "score": <number>,
  "max_score": <number>,
  "summary": "<overall feedback paragraph>",
  "questions": [
{chr(10).join(q_id_examples)}{q_id_rest}
  ]
}}

Each entry in "questions" must have: question_id (string matching the IDs above), earned (number), possible (number), feedback (string).
Do not include any text outside the JSON object."""


def build_quiz_message(student_name: str, answers: list) -> str:
    """Format a student's quiz answers for the AI.

    answers: list of {question_id, question_text, answer_text, points}
    """
    parts = [f"STUDENT: {student_name}\n"]
    for i, a in enumerate(answers, 1):
        parts.append(f"QUESTION {i} (ID {a['question_id']}, {a['points']} pts):")
        parts.append(f"{a['question_text']}\n")
        answer_text = a.get('answer_text') or '(no answer provided)'
        parts.append(f"STUDENT'S ANSWER:\n{answer_text}\n")
    return "\n".join(parts)


def build_review_prompt(rubric: str, all_results: list,
                        grading_mode: str = 'essay') -> tuple:
    """Build system + user prompts for the consistency review.

    Returns (system_prompt, user_message).
    all_results: list of graded result dicts from the session.
    """
    system_prompt = """You are an expert academic grading auditor performing a consistency review.

You have been given a rubric and the AI-generated scores and feedback for ALL students in a grading batch.

Your job is to review the EXISTING grades for internal consistency. Do NOT re-grade from scratch.

Check for:
1. SCORE CONSISTENCY — Similar quality answers should receive similar scores. Look for students with comparable feedback but significantly different scores.
2. FEEDBACK-SCORE ALIGNMENT — The score should match the tone and content of the feedback. Flag cases where glowing feedback accompanies a low score, or harsh feedback accompanies a high score.
3. OUTLIERS — Scores that are unusually high or low compared to peers with similar-quality work.
4. SCORING DRIFT — Scores that trend higher or lower as the batch progresses (first students graded differently than last students).

For each flagged student, suggest a corrected score and explain your reasoning.

You MUST respond in EXACTLY this JSON format:
{
  "overall_assessment": "<2-3 sentence summary of batch consistency — note any patterns>",
  "flags": [
    {
      "idx": <integer index of the student in the results array>,
      "filename": "<student name/filename>",
      "original_score": <number>,
      "suggested_score": <number>,
      "flag": "<one of: ok, low, high, inconsistent>",
      "rationale": "<1-2 sentence explanation>"
    }
  ]
}

Only include students that need attention in the flags array. If all scores look consistent, return an empty flags array. The "flag" field means:
- "high" — score appears too high relative to feedback quality or peer comparison
- "low" — score appears too low relative to feedback quality or peer comparison
- "inconsistent" — score and feedback contradict each other
- "ok" — included for reference but no change needed

Do not include any text outside the JSON object."""

    # Build compact student results summary
    parts = [f"RUBRIC:\n{rubric}\n"]
    parts.append(f"GRADING MODE: {'Quiz' if grading_mode == 'quiz' else 'Essay'}")
    parts.append(f"TOTAL STUDENTS: {len(all_results)}\n")
    parts.append("STUDENT RESULTS:")

    for i, r in enumerate(all_results):
        if r.get('error'):
            continue  # skip errored results

        name = r.get('filename', f'Student {i}')
        score = r.get('score', 0)
        max_score = r.get('max_score', 100)

        # Truncate summary for token efficiency
        summary = (r.get('summary') or '')[:200]

        line = f"\n[{i}] {name}: {score}/{max_score}"

        if grading_mode == 'quiz' and r.get('questions'):
            q_scores = ', '.join(
                f"Q{j+1}: {q['earned']}/{q['possible']}"
                for j, q in enumerate(r['questions'])
            )
            line += f"\n  Questions: {q_scores}"
        elif r.get('categories'):
            cat_scores = ', '.join(
                f"{c['name']}: {c['earned']}/{c['possible']}"
                for c in r['categories']
            )
            line += f"\n  Categories: {cat_scores}"

        line += f"\n  Feedback: {summary}"
        parts.append(line)

    user_message = "\n".join(parts)
    return (system_prompt, user_message)


def build_rubric_generation_prompt(description: str) -> str:
    return f"""You are an expert curriculum designer and educator. A teacher has described what they want in a grading rubric. Generate a clear, detailed, ready-to-use rubric based on their description.

TEACHER'S DESCRIPTION:
{description}

REQUIREMENTS FOR THE RUBRIC:
1. Break the rubric into 4-6 distinct criteria/categories.
2. Assign clear point values to each category that add up to the total points specified (default to 100 if not specified).
3. For each category, write 2-3 sentences describing what earns full credit, what earns partial credit, and what earns little/no credit.
4. Use clear, specific language a student could understand.
5. Format it so a grading AI can easily evaluate student work against it.

Respond with ONLY the rubric text itself — no preamble, no "Here is your rubric:", no extra commentary. Start directly with the first criterion."""
