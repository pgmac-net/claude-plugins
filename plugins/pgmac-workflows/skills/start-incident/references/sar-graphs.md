# SAR Graphs with `sargraph`

`sargraph` turns sysstat SAR data (binary `saDD` files or `sar -A` text) into graphs. Use it whenever the incident might involve resource utilisation or exhaustion — CPU saturation, memory pressure, swapping, disk I/O stalls, load spikes, network drops. A graph showing a resource hitting its ceiling at the moment symptoms began is better evidence than any single `top` snapshot, and it is the evidence `create-pir` needs to show root cause and recovery.

**Output SVG.** It stays sharp at any zoom, diffs as text, and renders directly in the MkDocs incidents site.

It runs locally (`~/.local/bin/sargraph`, from [pgmac-net/sargraph](https://github.com/pgmac-net/sargraph)), not on the affected host. If `command -v sargraph` finds nothing, install it with `uv tool install git+https://github.com/pgmac-net/sargraph`.

## When It Adds Value

| Graph it | Skip it |
|---|---|
| OOM kills, swapping, slow or hung processes | Clean config error, expired cert, bad deploy with an obvious cause |
| High load, PLEG stalls, kubelet/dqlite slowness | Failure is plainly network-path or DNS with healthy hosts |
| iowait, read-only remounts, iSCSI or NFS timeouts, disk-full | No SAR data covers the window (see below) |
| "It got slow" with no obvious cause | |
| Proving a fix worked: the metric returns to baseline | |

If unsure, graph it. It is read-only, cheap, and a flat line rules a resource out, which is also worth recording.

## 1. Get the SAR Data

SAR files live on the affected host, one per day of month: `/var/log/sysstat/saDD` (Debian/Ubuntu) or `/var/log/sa/saDD` (RHEL family). They rotate (typically 7–28 days), so **copy them early in the incident** — the pre-incident baseline is exactly what disappears first.

Keep evidence outside any repo while the incident is live:

```bash
EV=~/incident-evidence/incidents-<N>     # <N> = incident issue number
mkdir -p "$EV"
```

Export as text on the host. This sidesteps binary-format mismatches between the host's sysstat version and the local one:

```bash
ssh <host> 'LC_ALL=C sar -A -f /var/log/sysstat/sa08' > "$EV/<host>-sa08.txt"
```

Grab the previous day's file too when the incident started near midnight or the baseline matters. Same-version hosts can just `scp` the binary `saDD` files.

**No data?** sysstat may be installed but disabled (`ENABLED="false"` in `/etc/default/sysstat`), or retention may already have dropped the window. This has happened before on the k8s nodes. Note it as a timeline comment and a PIR action item ("no resource history for <host>"), then move on. Don't block the incident on it.

## 2. Find the Selectors

Selectors are `SECTION.COLUMN[@INSTANCE]` and use sar's own column names (`cpu.%usr`, not `%user`). **Always run `list` first**, because a wrong name fails with `no data matches ...`:

The full list runs to hundreds of lines (every core, disk, and interface), so view the columns first, then the instances of the one you want:

```bash
sargraph list "$EV"/<host>-sa08.txt | sed 's/@.*//' | sort -u        # columns
sargraph list "$EV"/<host>-sa08.txt | grep '^disk\.%util@' | grep -v loop   # instances
```

Globs work in `-m`: `'disk.%util@nvme*'`, `'swapping.pswp*'`, `'cpu.%*@all'`.

## 3. Pick Metrics by Symptom

| Suspected exhaustion | Metrics |
|---|---|
| CPU | `cpu.%usr@all` `cpu.%sys@all` `cpu.%iowait@all` `cpu.%steal@all` `load.ldavg-1` `load.runq-sz` |
| Memory / OOM | `mem.%memused` `mem.kbavail` `mem.%commit` `swap.%swpused` `swapping.pswpin/s` `paging.majflt/s` |
| Disk I/O | `disk.%util@<dev>` `disk.await@<dev>` `disk.aqu-sz@<dev>` `cpu.%iowait@all` |
| Network | `net.rxkB/s@<if>` `net.txkB/s@<if>` `net-err.rxdrop/s@<if>` `softnet.dropd/s` |
| Process / fd churn | `task.proc/s` `task.cswch/s` `load.plist-sz` `inode.file-nr` `sock.tcpsck` |
| Pressure stall (if collected) | `x-scpu-10.%scpu-10` `x-smem-10.%smem-10` `x-sio-10.%sio-10` |

Keep one graph to 3–5 related metrics. A stacked panel per metric (the default layout) reads better than a crowded single chart.

## 4. Graph It

```bash
sargraph graph "$EV"/<host>-sa07.txt "$EV"/<host>-sa08.txt \
  -m 'mem.%memused' -m 'swap.%swpused' -m 'paging.majflt/s' -m 'load.ldavg-1' \
  --from 2026-11-08T06:00 --to 2026-11-08T12:00 \
  --title '<host> memory pressure — <short incident label>' \
  -o "$EV"/<host>-memory.svg
```

- Pass several files to span days; they are stitched into one time series.
- `--from`/`--to` accept ISO datetime, date, or `HH:MM`. Frame the window as **baseline → onset → recovery**, roughly 1–2h either side of the incident, so the change is obvious.
- `--layout stacked` (default) is one panel per metric. `combined` puts everything on one axis (use it only for same-unit metrics, e.g. `%usr`/`%sys`/`%iowait`). `separate` writes one file per metric and needs `{metric}` or `{n}` in the `-o` name.
- Override the auto title (selector list + hostname) with something a reader understands.
- Repeat `-o` to get several formats in one pass. Add a `.png` alongside the `.svg` when you want to view it yourself in this session (the Read tool renders PNG, not SVG).

Times in the graph follow the host's SAR timestamps. Confirm they line up with the AEST timeline before drawing conclusions.

## 5. Record It

GitHub issue comments can't take file attachments from `gh`, so the timeline comment carries the finding in words plus the local path:

```markdown
**14:32 AEST** — sargraph on k8s03 (memory): %memused climbed 62→97% from 13:50, swap in from 14:05, majflt/s spiked at 14:20 when the OOM kills began. Graph: ~/incident-evidence/incidents-87/k8s03-memory.svg
```

After recovery, graph the same metrics over a window that includes the fix, so the return to baseline is captured. `create-pir` embeds these SVGs in the PIR.
