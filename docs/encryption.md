# The encrypted store

**The database is a cache. The sealed log is the only thing that must survive.**

That is one step further than the rule this project already had. `receipts`, `extractions`
and `corrections` were always append-only truth and `transactions` was always derived. Moving
truth out of SQLite and into age-sealed files on disk makes the *whole* database derived — so
it can live on tmpfs, be deleted at `spend lock`, and be rebuilt at `spend unlock`. Nothing
durable is ever plaintext.

```
~/.local/share/spend/            ciphertext only, forever
  log/<ab>/<sha256>.age          one sealed event per file, content-addressed
  blobs/<ab>/<sha256>.age        receipt originals, sealed individually
  drop/{inbox,archive,…}         CSV and OFX exports on their way in

~/.config/spend/
  recipients.txt                 age public keys — NOT secret
  identity.age                   the identity, wrapped in your passphrase — the secret
  simplefin.access               the Bridge credential, mode 0600
  accounts.toml                  which native account is which of yours

$XDG_RUNTIME_DIR/spend/          plaintext, tmpfs, 0700, gone at lock
  identity  spend.db  render/  agent/  expires_at  .lock
```

## What this protects, and what it does not

Protects: **the disk at rest**, the nightly backups, and anything copied off the box. Steal
this laptop powered off and you get age ciphertext.

Does **not** protect against: root on a running, unlocked box; a compromised `spend` process;
or the agent, which reads plaintext by design, because that is the entire feature.

## Before you trust any of it: fix swap

`tmpfs` pages are swappable. If swap is an unencrypted file — which is the Fedora default on
this hardware — then the decrypted ledger reaches the platter anyway and this whole design
protects a powered-off disk and very little else. `spend doctor` checks this on every run and
says so loudly.

Do it in this order, because the first command is what makes the second one safe:

```bash
sudo swapoff -a && free -h && sleep 300 && free -h   # does the box survive with no swap?
```

If it OOMs, swap is load-bearing and the real problem is memory pressure — fix that first. If
it survives, you have just proved that the worst failure mode of the next step (booting with
no swap) is survivable:

```bash
sudoedit /etc/fstab                                  # comment out the swapfile line
echo 'swapcrypt /var/swap/swapfile /dev/urandom swap,cipher=aes-xts-plain64,size=512' \
  | sudo tee -a /etc/crypttab
echo '/dev/mapper/swapcrypt none swap defaults 0 0' | sudo tee -a /etc/fstab
sudo systemctl daemon-reload && sudo reboot
swapon --show                                        # must be under /dev/mapper
```

**Why dm-crypt and not zram.** zram is the tidier answer on a box with headroom, and it is
what Fedora ships by default. This box has 7.3 GiB of RAM with a few hundred megabytes free
and gigabytes already in swap; moving that into compressed RAM costs more RAM than it has.
Adding zram *alongside* a lower-priority disk swap leaves plaintext able to reach the platter,
which is the whole problem. dm-crypt swap also covers the **process heap** — a decrypted
25 MB JPEG in a Python `bytes` is swappable no matter where the cache lives. It costs
hibernation, which this box does not use.

**btrfs.** `/home` is copy-on-write, so deleting a plaintext file does not reliably destroy
its blocks, and on an SSD neither does overwriting them. This only ever mattered once, at the
beginning, when there was no history yet. `spend init` refuses to run on top of an old
`~/.cache/spend`, which used to hold downscaled receipt JPEGs and their OCR text — a plaintext
receipt store that nothing about the word "cache" suggests is sensitive.

## Why one sealed file per event

Two properties fall out of it, and neither survives the obvious alternative of one segment
file per month.

**The sync job writes what it cannot read.** age encrypts to a *public* key. The nightly bank
pull holds `recipients.txt` and the Bridge credential and nothing else, so it appends sealed
events all night and is structurally incapable of reading one historical transaction. Appending
to a segment would mean decrypting it first, which would put the private key on a timer that
runs while you are asleep. `spend-feeds.service` carries
`ReadWritePaths=%h/.local/share/spend/log` and nothing else, so systemd enforces it rather
than a docstring asserting it.

**Re-appending is a byte-level no-op.** A file is named for the `sha256` of its record's
canonical JSON, *excluding* the timestamp, and the writer returns early when it already
exists. That is what makes SimpleFIN's recommended overlapping windows affordable: re-polling
thirty days every night for a year writes nothing after the first pass. It also means the
timestamp that persists is the first observation's, which is exactly what `first_seen_at`
should mean.

A pending charge that posts is *not* a no-op, correctly: its body differs, so it hashes
differently and lands as a second file. Both survive and the projection takes the newer.

**Do not "optimise" the log into segments.** It would quietly cost both.

## Keys

`spend init` makes two identities. The first is wrapped in your passphrase and lives in
`identity.age`. The second is printed once, is never written to disk, and belongs **on paper**
— it is a second age recipient, so it can open everything, and it is the only thing between a
forgotten passphrase and total, unrecoverable loss. There is no backdoor and no support line.

`spend backup` copies `identity.age` along with the ciphertext. Wrapped in a passphrase is
exactly what `age -p` output is for, and losing the only copy of it is a far likelier way to
lose everything than someone brute-forcing scrypt off a stolen drive.

The unlocked identity is a 0600 file inside a 0700 tmpfs directory, not the kernel keyring.
Against a process running as this uid a key file adds no exposure at all, because the fully
decrypted SQLite cache is sitting in the same directory. A keyring would buy unswappability
for thirty-two bytes next to a twenty-megabyte swappable cache, at the cost of a hand-rolled
syscall wrapper no test can exercise and which is unreachable from the container's user
namespace. A key-holding daemon under `mlockall` is the right answer for a system with *no*
plaintext cache; revisit it the day the cache moves back onto disk.

**There is no headless-boot mode and no stored passphrase.** A passphrase recoverable from the
disk is not a passphrase. If the box reboots at three in the morning, receipts queue, the feed
keeps appending, and the web app returns when a human types it in.

## An honest note about the Bridge credential

The SimpleFIN Access URL embeds HTTP Basic credentials and has to be readable while the store
is locked, or the nightly sync could not run. It is a 0600 file on an unencrypted disk.

`systemd-creds --user` would look like an improvement and mostly is not: without a TPM binding
it encrypts against a per-user key at `~/.config/systemd/user-credentials/*.credkey`, on the
same unencrypted disk as everything else, so a thief holding this disk can decrypt it either
way. Sealing it to the age recipient *would* fix it and would also require an unlock to sync,
which defeats the whole point.

So: use a **read-only** token, treat it as revocable rather than as secret, and know that it
is the one credential here that a stolen disk gives up. It grants read access to statements,
not to money.

## What runs while locked

| | locked | unlocked |
|---|---|---|
| `spend feeds sync` (timer) | **yes** — appends events it cannot read | yes |
| `spend backup` (timer) | **yes** — ciphertext needs no key | yes |
| the web app and the extraction worker | no | yes |
| `spend agent build` | no | yes |

## The concurrency hazard, restated

The README already said the compose path and the systemd path must never run at once, because
both carry the extraction worker and would send the same receipt to `sir` twice. Encryption
adds a worse one on top: `spend unlock` **deletes the SQLite cache**, and a second process
holding that file ends up writing into a deleted inode and losing its writes with no error at
all. `unlock` and `lock` take an exclusive `flock` and long-lived processes take a shared one,
which turns that into a loud failure — but it is still the wrong thing to do.
