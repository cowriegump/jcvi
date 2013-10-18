#!/usr/bin/env python
# -*- coding: UTF-8 -*-

"""
%prog blast_file --qbed query.bed --sbed subject.bed

Accepts bed format and blast file, and run several BLAST filters below::

* Local dup filter:
if the input is query.bed and subject.bed, the script files query.localdups
and subject.localdups are created containing the parent|offspring dups, as
inferred by subjects hitting same query or queries hitting same subject.

* C-score filter:
see supplementary info for sea anemone genome paper, formula::

    cscore(A,B) = score(A,B) /
         max(best score for A, best score for B)

Finally a blast.filtered file is created.
"""

import sys
import logging
import os.path as op
import itertools

from math import log10
from collections import defaultdict

from jcvi.formats.bed import Bed
from jcvi.formats.blast import BlastLine
from jcvi.utils.grouper import Grouper
from jcvi.utils.cbook import gene_name
from jcvi.compara.synteny import check_beds
from jcvi.apps.base import OptionParser, OptionGroup, debug
debug()


def blastfilter_main(blast_file, p, opts):

    qbed, sbed, qorder, sorder, is_self = check_beds(blast_file, p, opts)

    tandem_Nmax = opts.tandem_Nmax
    cscore = opts.cscore

    fp = file(blast_file)
    total_lines = sum(1 for line in fp)
    logging.debug("Load BLAST file `%s` (total %d lines)" % \
            (blast_file, total_lines))
    fp.seek(0)
    blasts = sorted([BlastLine(line) for line in fp], \
            key=lambda b: b.score, reverse=True)

    filtered_blasts = []
    seen = set()
    ostrip = opts.strip_names
    nwarnings = 0
    for b in blasts:
        query, subject = b.query, b.subject
        if query == subject:
            continue

        if ostrip:
            query, subject = gene_name(query), gene_name(subject)
        if query not in qorder:
            if nwarnings < 100:
                logging.warning("{0} not in {1}".format(query,
                    qbed.filename))
            elif nwarnings == 100:
                logging.warning("too many warnings.. suppressed")
            nwarnings += 1
            continue
        if subject not in sorder:
            if nwarnings < 100:
                logging.warning("{0} not in {1}".format(subject,
                    sbed.filename))
            elif nwarnings == 100:
                logging.warning("too many warnings.. suppressed")
            nwarnings += 1
            continue

        qi, q = qorder[query]
        si, s = sorder[subject]

        if is_self and qi > si:
            # move all hits to same side when doing self-self BLAST
            query, subject = subject, query
            qi, si = si, qi
            q, s = s, q

        key = query, subject
        if key in seen:
            continue
        seen.add(key)
        b.query, b.subject = key

        b.qi, b.si = qi, si
        b.qseqid, b.sseqid = q.seqid, s.seqid

        filtered_blasts.append(b)

    if not cscore is None:
        before_filter = len(filtered_blasts)
        logging.debug("running the cscore filter (cscore>=%.2f) .." % cscore)
        filtered_blasts = list(filter_cscore(filtered_blasts, cscore=cscore))
        logging.debug("after filter (%d->%d) .." % (before_filter,
            len(filtered_blasts)))

    if not tandem_Nmax is None:
        logging.debug("running the local dups filter (tandem_Nmax=%d) .." % \
                tandem_Nmax)

        qtandems = tandem_grouper(qbed, filtered_blasts,
                flip=True, tandem_Nmax=tandem_Nmax)
        standems = tandem_grouper(sbed, filtered_blasts,
                flip=False, tandem_Nmax=tandem_Nmax)

        qdups_fh = open(op.splitext(opts.qbed)[0] + ".localdups", "w") \
                if opts.tandems_only else None

        if is_self:
            for s in standems:
                qtandems.join(*s)
            qdups_to_mother = write_localdups(qtandems, qbed, qdups_fh)
            sdups_to_mother = qdups_to_mother
        else:
            qdups_to_mother = write_localdups(qtandems, qbed, qdups_fh)
            sdups_fh = open(op.splitext(opts.sbed)[0] + ".localdups", "w") \
                    if opts.tandems_only else None
            sdups_to_mother = write_localdups(standems, sbed, sdups_fh)

        if opts.tandems_only:
            # write out new .bed after tandem removal
            write_new_bed(qbed, qdups_to_mother)
            if not is_self:
                write_new_bed(sbed, sdups_to_mother)

            # just want to use this script as a tandem finder.
            sys.exit()

        before_filter = len(filtered_blasts)
        filtered_blasts = list(filter_tandem(filtered_blasts, \
                qdups_to_mother, sdups_to_mother))
        logging.debug("after filter (%d->%d) .." % \
                (before_filter, len(filtered_blasts)))

    blastfilteredfile = blast_file + ".filtered"
    fw = open(blastfilteredfile, "w")
    write_new_blast(filtered_blasts, fh=fw)
    fw.close()


def write_localdups(tandems, bed, dups_fh=None):

    tandem_groups = []
    for group in tandems:
        rows = [bed[i] for i in group]
        # within the tandem groups, genes are sorted with decreasing size
        rows.sort(key=lambda a: (-abs(a.end - a.start), a.accn))
        tandem_groups.append([x.accn for x in rows])

    dups_to_mother = {}
    n = 1
    for accns in sorted(tandem_groups):
        if dups_fh:
            print >>dups_fh, "\t".join(accns)
            if n:
                n -= 1
                logging.debug("write local dups to file %s" \
                        % dups_fh.name)

        for dup in accns[1:]:
            dups_to_mother[dup] = accns[0]

    return dups_to_mother


def write_new_bed(bed, children):
    # generate local dup removed annotation files
    out_name = "%s.nolocaldups%s" % op.splitext(bed.filename)
    logging.debug("write tandem-filtered bed file %s" % out_name)
    fh = open(out_name, "w")
    for i, row in enumerate(bed):
        if row['accn'] in children:
            continue
        print >>fh, row
    fh.close()


def write_new_blast(filtered_blasts, fh=sys.stdout):
    for b in filtered_blasts:
        print >> fh, b


def filter_cscore(blast_list, cscore=.5):

    best_score = defaultdict(float)
    for b in blast_list:
        if b.score > best_score[b.query]:
            best_score[b.query] = b.score
        if b.score > best_score[b.subject]:
            best_score[b.subject] = b.score

    for b in blast_list:
        cur_cscore = b.score / max(best_score[b.query], best_score[b.subject])
        if cur_cscore > cscore:
            yield b


def filter_tandem(blast_list, qdups_to_mother, sdups_to_mother):

    mother_blast = []
    for b in blast_list:
        if b.query in qdups_to_mother:
            b.query = qdups_to_mother[b.query]
        if b.subject in sdups_to_mother:
            b.subject = sdups_to_mother[b.subject]
        mother_blast.append(b)

    mother_blast.sort(key=lambda b: b.score, reverse=True)
    seen = {}
    for b in mother_blast:
        if b.query == b.subject:
            continue
        key = b.query, b.subject
        if key in seen:
            continue
        seen[key] = None
        yield b


def tandem_grouper(bed, blast_list, tandem_Nmax=10, flip=True):
    if not flip:
        simple_blast = [(b.query, (b.sseqid, b.si)) \
                for b in blast_list if b.evalue < 1e-10]
    else:
        simple_blast = [(b.subject, (b.qseqid, b.qi)) \
                for b in blast_list if b.evalue < 1e-10]

    simple_blast.sort()

    standems = Grouper()
    for name, hits in itertools.groupby(simple_blast, key=lambda x: x[0]):
        # these are already sorted.
        hits = [x[1] for x in hits]
        for ia, a in enumerate(hits[:-1]):
            b = hits[ia + 1]
            # on the same chr and rank difference no larger than tandem_Nmax
            if b[1] - a[1] <= tandem_Nmax and b[0] == a[0]:
                standems.join(a[1], b[1])

    return standems


def main(args):

    p = OptionParser(__doc__)
    p.set_beds()
    p.set_stripnames()
    p.add_option("--tandems_only", dest="tandems_only",
            action="store_true", default=False,
            help="only calculate tandems, write .localdup file and exit.")
    p.add_option("--tandem_Nmax", type="int",
            help="merge tandem genes within distance [default: %default]")
    p.add_option("--cscore", type="float",
            help="retain hits that have good bitscore. a value of 0.5 means "
                 "keep all values that are 50% or greater of the best hit. "
                 "higher is more stringent [default: %default]")

    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    blastfile, = args
    blastfilter_main(blastfile, p, opts)


if __name__ == "__main__":
    main(sys.argv[1:])