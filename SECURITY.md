# Security

Please do not open a public issue for a security problem. Use **Security, Report a vulnerability** on this repository's page: the report is private.

Say what happens, how to make it happen, and what it would allow. Reports about these count in particular:

- a message that drives a terminal, or looks like anything but a message;
- a message, a key or a name that reads or writes outside the store;
- a job with two takers, or a message that is lost;
- a secret that is sent although it should have been refused.

The sender's name is not proof, by design: any program running as you can write to the store. Only the newest `main` is supported; there is no release yet.
