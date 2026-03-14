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


def build_system_prompt(rubric: str, strictness: int = DEFAULT_STRICTNESS) -> str:
    info = get_strictness_info(strictness)
    return f"""You are an experienced academic grading assistant. You will grade student essays according to the rubric provided below.

RUBRIC:
{rubric}

GRADING MODE: {info['emoji']} {info['label']} (Level {info['level']}/10)
{info['instructions']}

INSTRUCTIONS:
1. Read the essay carefully and thoroughly.
2. Evaluate it against EACH criterion in the rubric, applying the grading mode above.
3. For every rubric criterion, determine: the criterion name, points earned, points possible, and a 1-2 sentence explanation that references SPECIFIC content from the student's essay. Do NOT give generic feedback like "your argument could be stronger." Instead, cite a specific claim, example, or passage the student wrote and explain what worked or what was missing. For example: "While you mention the Battle of Midway, you don't fully explain what made it a turning point" or "Your comparison of photosynthesis to a factory assembly line was a strong analogy that showed real understanding."
4. Sum the category scores to get the total score. Determine max_score from the rubric.
5. Write a 3-5 sentence overall feedback paragraph addressed directly to the student (use "you/your"). Start with something specific they did well (reference their actual writing), then explain where points were lost by pointing to specific examples, claims, or sections in their essay that fell short. Every piece of feedback must connect to something the student actually wrote.
6. Your tone and strictness MUST reflect the grading mode specified above.

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
