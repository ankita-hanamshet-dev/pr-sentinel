def build(rows, out=[]):
    f = open("/tmp/report.txt", "w")
    for r in rows:
        try:
            f.write(str(r["total"]) + "\n")
        except:
            pass
    out.append(len(rows))
    return out