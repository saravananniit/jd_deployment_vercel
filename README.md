'''
# Optional. Copy to .env for local runs. Do not commit .env.
# The provider, model name and Groq API key are entered in the web page, not here.

# Which providers the page offers. Locally: both. On a deployed server: groq only.
ENABLED_PROVIDERS=ollama,groq
# Provider preselected in the page (defaults to the first enabled one).
LLM_PROVIDER=ollama

# Ollama server settings and the default model name shown in the page.
OLLAMA_URL=http://localhost:11434
OLLAMA_MODEL=gemma3:1b

# Optional: prefill the Groq model name in the page (users can still change it).
# GROQ_MODEL=name-of-a-model-from-the-groq-console
'''
