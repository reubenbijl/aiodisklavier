# Security

## What this library is up against

The piano's HTTP API is plaintext and, on a piano with no passcode set, unauthenticated. Its
SMB share is served to guests with write access, over SMB1, which has no meaningful integrity
protection. SSDP discovery answers are unauthenticated multicast. None of that is this
library's to fix: the network the piano sits on is the security boundary, and the
[README](README.md#security) says so.

What this library does take responsibility for is how it behaves when the thing on the other
end is not the piano, or is a piano that has been interfered with:

- response bodies are read against a size ceiling, never without limit;
- redirects are refused rather than followed;
- strings the device supplies are treated as data — never interpolated into a path, and
  escaped before an example prints them;
- share paths containing `..` or control characters are refused before they reach the wire,
  and listing rows that are not plain filenames are dropped, so a path this library hands
  back cannot escape the directory a caller joins it onto;
- `python -m aiodisklavier` reports the shape of the piano's state and not its contents,
  because that state includes an account email address and a passcode.

A way to defeat any of those is a vulnerability, and so is anything that lets a hostile
device crash, hang, exhaust or mislead a program that uses this library.

## Reporting one

Please do not open a public issue with the details.

Use GitHub's private reporting: on the repository's **Security** tab, choose **Report a
vulnerability**. That opens a private advisory visible only to you and the maintainer.

If that option is not offered, open an ordinary issue that says only that you have a security
report to make — no details — and you will be contacted to arrange a private channel.

You should hear back within a week. This is a small project maintained in spare time, so a
fix may take longer than that, but you will be told what is happening, and credited in the
release notes unless you would rather not be.

## Supported versions

Fixes go into the latest release. There are no maintenance branches.
