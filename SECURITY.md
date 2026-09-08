# Security Policy

Veridex is designed to operate around sensitive QA environments, so safe defaults matter.

## Please do not report secrets in public

Do not include access tokens, passwords, private endpoints, real customer identifiers, UAT/production dumps, or confidential requirements and attachments.

If you find a security issue, contact the repository owner privately rather than opening a public issue containing exploit details or secrets.

## Trust boundary

Guarded adapters can enforce Veridex policy only when test code uses them. For hard isolation, use dedicated QA credentials, database permissions, network controls, or proxies that independently restrict writes.
