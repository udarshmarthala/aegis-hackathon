"""The brain: which model chooses the tools for one horizon step.

``router.BrainRouter`` is the only public entry point. It tries Bedrock, then
Gemini's brain key pool, then the scripted policy, and labels the decision with
the tier that actually answered. ``compactor_llm.GeminiCompactorLLM`` is the
compactor's language model and draws on a separate key pool.
"""
