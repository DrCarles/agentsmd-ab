# Secure coding checklist

**Hardcoding Secrets**
- [ ] Do not hard-code passwords, API keys, tokens, or any other credentials directly into the source code.

**Application-Level Logging**
- [ ] Ensure log traces for critical system events clearly identify the actor, the action performed, and the precise timestamp.
- [ ] Avoid logging personally identifiable information (PII), passwords, tokens, private keys, internal secrets, or the contents/names of user files.

**Exception Handling**
- [ ] When handling software exceptions, always ensure the application fails in a secure state and presents a safe, generic error message to the end-user.
- [ ] Log all application-logic errors. 