# Exceptions

All exceptions inherit from `WizError`.

```
WizError
├── WizAuthenticationError
├── WizAPIError
├── WizConfigurationError
├── WizCredentialsError
├── WizRateLimitError
├── WizQueryError
│   └── WizSchemaValidationError
├── WizReportError        # deprecated; never raised, not exported from `wizsec`
├── WizTimeoutError
├── WizFileError
└── WizServerlessError
```

::: wizsec.exceptions
    options:
      show_root_heading: false
      members_order: source
