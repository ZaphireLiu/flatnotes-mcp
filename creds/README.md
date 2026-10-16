# Credentials

One `.env` file per flatnotes instance, named after the `credentials_file`
entry in `config/servers.yaml` (typically `/creds/<server-id>.env`).

Copy an instance's own flatnotes `.env` here, or create a minimal one:

```dotenv
FLATNOTES_USERNAME=youruser
FLATNOTES_PASSWORD=yourpassword
```

Only `FLATNOTES_USERNAME` and `FLATNOTES_PASSWORD` are read; the rest of the
file is ignored. These files are mounted read-only into the container and are
never written to, logged, or returned by any tool.

`*.env` in this directory is git-ignored.
