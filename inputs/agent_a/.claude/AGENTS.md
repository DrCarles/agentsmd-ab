# Secure coding checklist

**Hardcoding Secrets**
- [ ] Do not hard-code passwords, API keys, tokens, or any other credentials directly into the source code.

**Third-Party Components & Dependencies**
- [ ] When adding a new dependency, prioritize actively maintained components over versions with known high or critical vulnerabilities of a dependency.
- [ ] When adding a new dependency, prioritize the latest stable release of the components by default.
- [ ] When adding a new dependency, pin component version using lockfiles.

**Authentication**
- [ ] Log all application-layer successful and unsuccessful authentication attempts.

**Authorization**
- [ ] Enforce authorization at the business logic layer, not exclusively in the presentation layer.
- [ ] Base authorization decisions on non-user-manipulable claims.
- [ ] Log all application-layer successful and unsuccessful authorization attempts. 

**Data in Transit**
- [ ] Prioritize the use of TLS for every external network connection. Avoid disabling certificate validation or fall back to an insecure protocol.
- [ ] For user-provided URLs block private/link-local IPs after DNS resolution to prevent SSRF attacks.

**Untrusted Input and Safe Command Invocation** 
- [ ] Treat all external data as potentially malicious. Validate and sanitize data, and never execute, evaluate, or load code from user-provided files.
- [ ] Do not use string concatenation to build executable OS commands or SQL queries. Always rely on parameterized inputs, prepared statements, and safe APIs.
- [ ] Log all application-layer OS command invocations that use user-provided data as arguments.

**Safe file handling**
- [ ] Sanitize filenames for external files stored on the filesystem to prevent path traversal attacks.

**Outbound**
- [ ] Enforce HTTP security headers and define a strict CORS policy for all web applications.

**Application-Level Logging**
- [ ] Ensure log traces for critical system events clearly identify the actor, the action performed, and the precise timestamp.
- [ ] Avoid logging personally identifiable information (PII), passwords, tokens, private keys, internal secrets, or the contents/names of user files.

**Exception Handling**
- [ ] When handling software exceptions, always ensure the application fails in a secure state and presents a safe, generic error message to the end-user.
- [ ] Log all application-logic errors. 