# -*- coding: utf-8 -*-
"""
Test AI API connection.
Run: python test_ai.py
"""
import anthropic

client = anthropic.Anthropic(auth_token="sk-ff302634a0eddc60fc5a4f3f3b7e60165cbf1587c479c989b8fb3a20fd026989",
                          base_url="https://api.cooker.club"
                          )
message = client.messages.create(
    model="claude-haiku-4-5",
    messages=[
        {"role": "user", "content": "What is the meaning of life?"}
    ],
    max_tokens=10000,
)
print(message.content)