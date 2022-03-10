#!/usr/bin/env python
# -*- coding: utf-8  -*-
import sys
import ast
import warnings
import pathlib

import click

import numpy as np
import pandas as pd

import scipy.spatial
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from . import _dedup, _fileio, _pairsam_format, _headerops, cli, common_io_options
from .pairtools_markasdup import mark_split_pair_as_dup
from .pairtools_stats import PairCounter


import time

UTIL_NAME = "pairtools_dedup"

# you don't need to load more than 10k lines at a time b/c you get out of the
# CPU cache, so this parameter is not adjustable
MAX_LEN = 10000


@cli.command()
@click.argument("pairs_path", type=str, required=False)
@click.option(
    "-o",
    "--output",
    type=str,
    default="",
    help="output file for pairs after duplicate removal."
    " If the path ends with .gz or .lz4, the output is bgzip-/lz4c-compressed."
    " By default, the output is printed into stdout.",
)
@click.option(
    "--output-dups",
    type=str,
    default="",
    help="output file for duplicated pairs. "
    " If the path ends with .gz or .lz4, the output is bgzip-/lz4c-compressed."
    " If the path is the same as in --output or -, output duplicates together "
    " with deduped pairs. By default, duplicates are dropped.",
)
@click.option(
    "--output-unmapped",
    type=str,
    default="",
    help="output file for unmapped pairs. "
    "If the path ends with .gz or .lz4, the output is bgzip-/lz4c-compressed. "
    "If the path is the same as in --output or -, output unmapped pairs together "
    "with deduped pairs. If the path is the same as --output-dups, output "
    "unmapped reads together with dups. By default, unmapped pairs are dropped.",
)
@click.option(
    "--output-stats",
    type=str,
    default="",
    help="output file for duplicate statistics. "
    " If file exists, it will be open in the append mode."
    " If the path ends with .gz or .lz4, the output is bgzip-/lz4c-compressed."
    " By default, statistics are not printed.",
)
@click.option(
    "--max-mismatch",
    type=int,
    default=3,
    show_default=True,
    help="Pairs with both sides mapped within this distance (bp) from each "
    "other are considered duplicates.",
)
@click.option(
    "--method",
    type=click.Choice(["max", "sum"]),
    default="max",
    help="define the mismatch as either the max or the sum of the mismatches of"
    "the genomic locations of the both sides of the two compared molecules",
    show_default=True,
)
@click.option(
    "--chunksize",
    type=int,
    default=3100000,
    show_default=True,
    help="Number of pairs in each chunk. Reduce for lower memory footprint",
)
@click.option(
    "--sep",
    type=str,
    default=_pairsam_format.PAIRSAM_SEP_ESCAPE,
    help=r"Separator (\t, \v, etc. characters are " "supported, pass them in quotes) ",
)
@click.option(
    "--comment-char", type=str, default="#", help="The first character of comment lines"
)
@click.option(
    "--send-header-to",
    type=click.Choice(["dups", "dedup", "both", "none"]),
    default="both",
    help="Which of the outputs should receive header and comment lines",
)
@click.option(
    "--c1",
    type=int,
    default=_pairsam_format.COL_C1,
    help="Chrom 1 column; default {}".format(_pairsam_format.COL_C1),
)
@click.option(
    "--c2",
    type=int,
    default=_pairsam_format.COL_C2,
    help="Chrom 2 column; default {}".format(_pairsam_format.COL_C2),
)
@click.option(
    "--p1",
    type=int,
    default=_pairsam_format.COL_P1,
    help="Position 1 column; default {}".format(_pairsam_format.COL_P1),
)
@click.option(
    "--p2",
    type=int,
    default=_pairsam_format.COL_P2,
    help="Position 2 column; default {}".format(_pairsam_format.COL_P2),
)
@click.option(
    "--s1",
    type=int,
    default=_pairsam_format.COL_S1,
    help="Strand 1 column; default {}".format(_pairsam_format.COL_S1),
)
@click.option(
    "--s2",
    type=int,
    default=_pairsam_format.COL_S2,
    help="Strand 2 column; default {}".format(_pairsam_format.COL_S2),
)
@click.option(
    "--unmapped-chrom",
    type=str,
    default=_pairsam_format.UNMAPPED_CHROM,
    help="Placeholder for a chromosome on an unmapped side; default {}".format(
        _pairsam_format.UNMAPPED_CHROM
    ),
)
@click.option(
    "--mark-dups",
    is_flag=True,
    help='If specified, duplicate pairs are marked as DD in "pair_type" and '
    "as a duplicate in the sam entries.",
)
@click.option(
    "--extra-col-pair",
    nargs=2,
    # type=click.Tuple([str, str]),
    multiple=True,
    help="Extra columns that also must match for two pairs to be marked as "
    "duplicates. Can be either provided as 0-based column indices or as column "
    'names (requires the "#columns" header field). The option can be provided '
    "multiple times if multiple column pairs must match. "
    'Example: --extra-col-pair "phase1" "phase2"',
)
@click.option(
    "--save-parent-id",
    is_flag=True,
    help="If specified, duplicate pairs are marked with the readID of the retained"
    " deduped read in the 'parent_readID' field."
    " Only has effect with scipy or sklearn backend",
)
@click.option(
    "--backend",
    type=click.Choice(["cython", "scipy", "sklearn"]),
    default="scipy",
    help="What backend to use",
)
@click.option(
    "-p",
    "--n-proc",
    type=int,
    default=1,
    help="Number of cores to use. Only applies with sklearn backend."
    "Still needs testing whether it is ever useful.",
)
@common_io_options
def dedup(
    pairs_path,
    output,
    output_dups,
    output_unmapped,
    output_stats,
    chunksize,
    max_mismatch,
    method,
    sep,
    comment_char,
    send_header_to,
    c1,
    c2,
    p1,
    p2,
    s1,
    s2,
    unmapped_chrom,
    mark_dups,
    extra_col_pair,
    save_parent_id,
    backend,
    n_proc,
    **kwargs,
):
    """Find and remove PCR/optical duplicates.

    Find PCR duplicates in an upper-triangular flipped sorted pairs/pairsam
    file. Allow for a +/-N bp mismatch at each side of duplicated molecules.

    PAIRS_PATH : input triu-flipped sorted .pairs or .pairsam file.  If the
    path ends with .gz/.lz4, the input is decompressed by bgzip/lz4c.
    By default, the input is read from stdin.
    """

    dedup_py(
        pairs_path,
        output,
        output_dups,
        output_unmapped,
        output_stats,
        chunksize,
        max_mismatch,
        method,
        sep,
        comment_char,
        send_header_to,
        c1,
        c2,
        p1,
        p2,
        s1,
        s2,
        unmapped_chrom,
        mark_dups,
        extra_col_pair,
        save_parent_id,
        backend,
        n_proc,
        **kwargs,
    )


def dedup_py(
    pairs_path,
    output,
    output_dups,
    output_unmapped,
    output_stats,
    chunksize,
    max_mismatch,
    method,
    sep,
    comment_char,
    send_header_to,
    c1,
    c2,
    p1,
    p2,
    s1,
    s2,
    unmapped_chrom,
    mark_dups,
    extra_col_pair,
    save_parent_id,
    backend,
    n_proc,
    **kwargs,
):
    sep = ast.literal_eval('"""' + sep + '"""')
    send_header_to_dedup = send_header_to in ["both", "dedup"]
    send_header_to_dup = send_header_to in ["both", "dups"]

    instream = (
        _fileio.auto_open(
            pairs_path,
            mode="r",
            nproc=kwargs.get("nproc_in"),
            command=kwargs.get("cmd_in", None),
        )
        if pairs_path
        else sys.stdin
    )
    outstream = (
        _fileio.auto_open(
            output,
            mode="w",
            nproc=kwargs.get("nproc_out"),
            command=kwargs.get("cmd_out", None),
        )
        if output
        else sys.stdout
    )
    out_stats_stream = (
        _fileio.auto_open(
            output_stats,
            mode="w",
            nproc=kwargs.get("nproc_out"),
            command=kwargs.get("cmd_out", None),
        )
        if output_stats
        else None
    )

    # generate empty PairCounter if stats output is requested:
    out_stat = PairCounter() if output_stats else None

    if not output_dups:
        outstream_dups = None
    elif output_dups == "-" or (
        pathlib.Path(output_dups).absolute() == pathlib.Path(output).absolute()
    ):
        outstream_dups = outstream
    else:
        outstream_dups = _fileio.auto_open(
            output_dups,
            mode="w",
            nproc=kwargs.get("nproc_out"),
            command=kwargs.get("cmd_out", None),
        )

    if not output_unmapped:
        outstream_unmapped = None
    elif output_unmapped == "-" or (
        pathlib.Path(output_unmapped).absolute() == pathlib.Path(output).absolute()
    ):
        outstream_unmapped = outstream
    elif (
        pathlib.Path(output_unmapped).absolute() == pathlib.Path(output_dups).absolute()
    ):
        outstream_unmapped = outstream_dups
    else:
        outstream_unmapped = _fileio.auto_open(
            output_unmapped,
            mode="w",
            nproc=kwargs.get("nproc_out"),
            command=kwargs.get("cmd_out", None),
        )

    header, body_stream = _headerops.get_header(instream)
    header = _headerops.append_new_pg(header, ID=UTIL_NAME, PN=UTIL_NAME)
    if send_header_to_dedup:
        outstream.writelines((l + "\n" for l in header))
    if send_header_to_dup and outstream_dups and (outstream_dups != outstream):
        dups_header = header
        dups_header[-1] += " parent_readID"
        outstream_dups.writelines((l + "\n" for l in dups_header))
    if (
        outstream_unmapped
        and (outstream_unmapped != outstream)
        and (outstream_unmapped != outstream_dups)
    ):
        outstream_unmapped.writelines((l + "\n" for l in header))

    column_names = _headerops.extract_column_names(header)
    extra_cols1 = []
    extra_cols2 = []
    if extra_col_pair is not None:
        for col1, col2 in extra_col_pair:
            extra_cols1.append(
                int(col1) if col1.isdigit() else column_names.index(col1)
            )
            extra_cols2.append(
                int(col2) if col2.isdigit() else column_names.index(col2)
            )

    if backend == "cython":
        streaming_dedup_cython(
            method,
            max_mismatch,
            sep,
            c1,
            c2,
            p1,
            p2,
            s1,
            s2,
            extra_cols1,
            extra_cols2,
            unmapped_chrom,
            body_stream,
            outstream,
            outstream_dups,
            outstream_unmapped,
            out_stat,
            mark_dups,
        )
    elif backend in ("scipy", "sklearn"):
        streaming_dedup(
            in_stream=instream,
            colnames=column_names,
            chunksize=chunksize,
            method=method,
            mark_dups=mark_dups,
            max_mismatch=max_mismatch,
            extra_col_pairs=list(extra_col_pair),
            unmapped_chrom=unmapped_chrom,
            comment_char=comment_char,
            outstream=outstream,
            outstream_dups=outstream_dups,
            outstream_unmapped=outstream_unmapped,
            save_parent_id=save_parent_id,
            out_stat=out_stat,
            backend=backend,
            n_proc=n_proc,
        )
    else:
        raise ValueError("Unknown backend")

    # save statistics to a file if it was requested:
    if out_stat:
        out_stat.save(out_stats_stream)

    if instream != sys.stdin:
        instream.close()

    if outstream != sys.stdout:
        outstream.close()

    if outstream_dups and (outstream_dups != outstream):
        outstream_dups.close()

    if (
        outstream_unmapped
        and (outstream_unmapped != outstream)
        and (outstream_unmapped != outstream_dups)
    ):
        outstream_unmapped.close()

    if out_stats_stream:
        out_stats_stream.close()


def fetchadd(key, mydict):
    key = key.strip()
    if key not in mydict:
        mydict[key] = len(mydict)
    return mydict[key]


def ar(mylist, val):
    return np.array(mylist, dtype={8: np.int8, 16: np.int16, 32: np.int32}[val])


def dedup_chunk(
    df,
    r,
    method,
    keep_parent_read_id,
    extra_col_pairs,
    backend,
    unmapped_chrom="!",
    n_proc=1,
):
    """Mark duplicates in a dataframe of pairs

    Parameters
    ----------
    df : pd.DataFrame
        Dataframe with pairs, has to contain columns 'chrom1', 'pos1', 'chrom2', 'pos2'
        'strand1', 'strand2'
    r : int
        Allowed distance between two pairs to call them duplicates
    method : str
        'sum' or 'max' - whether 'r' uses sum of distances on two ends of pairs, or the
        maximal distance
    keep_parent_read_id : bool
        If True, the read ID of the read that was not labelled as a duplicate from a
        group of duplicates is recorded for each read marked as duplicate.
        Only possible with non-cython backends
    extra_col_pairs : list of tuples
        List of extra column pairs that need to match between two reads for them be
        considered duplicates (e.g. useful if alleles are annotated)
    backend : str
        'scipy', 'sklearn', 'cython'
    unmapped_chrom : str, optional
        Which character denotes unmapped reads in the chrom1/chrom2 fields,
        by default "!"
    n_proc : int, optional
        How many cores to use, by default 1
        Only works for 'sklearn' backend

    Returns
    -------
    pd.DataFrame
        Dataframe with marked duplicates (extra boolean field 'duplicate'), and
        optionally recorded 'parent_readID'

    """
    if method not in ("max", "sum"):
        raise ValueError('Unknown method, only "sum" or "max" allowed')
    if backend == "sklearn":
        from sklearn import neighbors

    if method == "sum":
        p = 1
    else:
        p = np.inf

    unmapped_id = (df["chrom1"] == unmapped_chrom) | (df["chrom2"] == unmapped_chrom)
    unmapped = df[unmapped_id]

    df = df[~unmapped_id]
    N = df.shape[0]
    if N > 0:
        if backend == "sklearn":
            a = neighbors.radius_neighbors_graph(
                df[["pos1", "pos2"]], radius=r, p=p, n_jobs=n_proc,
            )
            a0, a1 = a.nonzero()
        elif backend == "scipy":
            z = scipy.spatial.cKDTree(df[["pos1", "pos2"]])
            a = z.query_pairs(r=r, p=p, output_type="ndarray")
            a0 = a[:, 0]
            a1 = a[:, 1]
        need_to_match = np.array(
            [
                ("chrom1", "chrom1"),
                ("chrom2", "chrom2"),
                ("strand1", "strand1"),
                ("strand2", "strand2"),
            ]
            + extra_col_pairs
        )
        nonpos_matches = np.all(
            [
                df.iloc[a0, df.columns.get_loc(lc)].values
                == df.iloc[a1, df.columns.get_loc(rc)].values
                for (lc, rc) in need_to_match
            ],
            axis=0,
        )
        a0 = a0[nonpos_matches]
        a1 = a1[nonpos_matches]
        a_mat = coo_matrix((np.ones_like(a0), (a0, a1)), shape=(N, N))

        df["clusterid"] = connected_components(a_mat, directed=False)[1]

    else:
        df["clusterid"] = np.nan
    dups = df["clusterid"].duplicated()
    df["duplicate"] = False
    if keep_parent_read_id:
        df["parent_readID"] = df["clusterid"].map(
            df[~dups].set_index("clusterid")["readID"]
        )
        unmapped["parent_readID"] = ""
    unmapped["duplicate"] = False

    df.iloc[dups, df.columns.get_loc("duplicate")] = True
    return pd.concat([unmapped, df.drop(["clusterid"], axis=1)]).reset_index(drop=True)


def _dedup_by_chunk(
    in_stream,
    colnames,
    method,
    chunksize,
    mark_dups,
    max_mismatch,
    extra_col_pairs,
    save_parent_id,
    comment_char,
    backend,
    n_proc,
):
    dfs = pd.read_table(
        in_stream, comment=comment_char, names=colnames, chunksize=chunksize
    )

    old_nodups = pd.DataFrame([])
    old_i = 0
    for df in dfs:
        marked = dedup_chunk(
            pd.concat([old_nodups, df], axis=0, ignore_index=True).reset_index(
                drop=True
            ),
            r=max_mismatch,
            method=method,
            keep_parent_read_id=save_parent_id,
            extra_col_pairs=extra_col_pairs,
            backend=backend,
            n_proc=n_proc,
        )
        marked = marked.iloc[old_i:, :].reset_index(drop=True)
        if mark_dups:
            marked.iloc[marked["duplicate"], marked.columns.get_loc("pair_type")] = "DD"
        nodups = marked[~marked["duplicate"]]

        nodups = nodups[colnames]
        i = max(nodups.shape[0] // 100, 100)
        old_nodups = nodups.iloc[-i:].reset_index(drop=True)
        old_i = i
        yield marked


def streaming_dedup(
    in_stream,
    colnames,
    chunksize,
    method,
    mark_dups,
    max_mismatch,
    extra_col_pairs,
    unmapped_chrom,
    comment_char,
    outstream,
    outstream_dups,
    outstream_unmapped,
    save_parent_id,
    out_stat,
    backend,
    n_proc,
):
    deduped_chunks = _dedup_by_chunk(
        in_stream=in_stream,
        colnames=colnames,
        method=method,
        chunksize=chunksize,
        mark_dups=mark_dups,
        max_mismatch=max_mismatch,
        extra_col_pairs=extra_col_pairs,
        save_parent_id=save_parent_id,
        comment_char=comment_char,
        backend=backend,
        n_proc=n_proc,
    )
    t0 = time.time()
    N = 0
    for chunk in deduped_chunks:
        N += chunk.shape[0]
        if out_stat is not None:
            out_stat.add_pairs_from_dataframe(chunk, unmapped_chrom=unmapped_chrom)
        mapped = np.logical_and(
            (chunk["chrom1"] != unmapped_chrom), (chunk["chrom2"] != unmapped_chrom)
        )
        duplicates = chunk["duplicate"]
        chunk = chunk.drop(columns=["duplicate"])
        if outstream_dups:
            chunk[mapped & duplicates].to_csv(
                outstream_dups, index=False, header=False, sep="\t"
            )
        if save_parent_id:
            chunk = chunk.drop(columns=["parent_readID"])
        chunk[mapped & (~duplicates)].to_csv(
            outstream, index=False, header=False, sep="\t"
        )
        if outstream_unmapped:
            chunk[~mapped].to_csv(
                outstream_unmapped, index=False, header=False, sep="\t"
            )
    t1 = time.time()
    t = t1 - t0
    print(f"total time: {t}")
    print(f"time per mln pairs: {t/N*1e6}")


def streaming_dedup_cython(
    method,
    max_mismatch,
    sep,
    c1ind,
    c2ind,
    p1ind,
    p2ind,
    s1ind,
    s2ind,
    extra_cols1,
    extra_cols2,
    unmapped_chrom,
    instream,
    outstream,
    outstream_dups,
    outstream_unmapped,
    out_stat,
    mark_dups,
):

    maxind = max(c1ind, c2ind, p1ind, p2ind, s1ind, s2ind)
    if bool(extra_cols1) and bool(extra_cols2):
        maxind = max(maxind, max(extra_cols1), max(extra_cols2))

    all_scols1 = [s1ind] + extra_cols1
    all_scols2 = [s2ind] + extra_cols2

    # if we do stats in the dedup, we need PAIR_TYPE
    # i do not see way around this:
    if out_stat:
        ptind = _pairsam_format.COL_PTYPE
        maxind = max(maxind, ptind)

    dd = _dedup.OnlineDuplicateDetector(method, max_mismatch, returnData=False)

    c1 = []
    c2 = []
    p1 = []
    p2 = []
    s1 = []
    s2 = []
    line_buffer = []
    cols_buffer = []
    chromDict = {}
    strandDict = {}
    n_unmapped = 0
    n_dups = 0
    n_nodups = 0
    curMaxLen = max(MAX_LEN, dd.getLen())

    t0 = time.time()
    N = 0

    instream = iter(instream)
    while True:
        rawline = next(instream, None)
        stripline = rawline.strip() if rawline else None

        # take care of empty lines not at the end of the file separately
        if rawline and (not stripline):
            warnings.warn("Empty line detected not at the end of the file")
            continue

        if stripline:
            cols = stripline.split(sep)
            if len(cols) <= maxind:
                raise ValueError(
                    "Error parsing line {}: ".format(stripline)
                    + " expected {} columns, got {}".format(maxind, len(cols))
                )

            if (cols[c1ind] == unmapped_chrom) or (cols[c2ind] == unmapped_chrom):

                if outstream_unmapped:
                    outstream_unmapped.write(stripline)
                    # don't forget terminal newline
                    outstream_unmapped.write("\n")

                # add a pair to PairCounter if stats output is requested:
                if out_stat:
                    out_stat.add_pair(
                        cols[c1ind],
                        int(cols[p1ind]),
                        cols[s1ind],
                        cols[c2ind],
                        int(cols[p2ind]),
                        cols[s2ind],
                        cols[ptind],
                    )
            else:
                line_buffer.append(stripline)
                cols_buffer.append(cols)

                c1.append(fetchadd(cols[c1ind], chromDict))
                c2.append(fetchadd(cols[c2ind], chromDict))
                p1.append(int(cols[p1ind]))
                p2.append(int(cols[p2ind]))
                if bool(extra_cols1) and bool(extra_cols2):
                    s1.append(
                        fetchadd("".join(cols[i] for i in all_scols1), strandDict)
                    )
                    s2.append(
                        fetchadd("".join(cols[i] for i in all_scols2), strandDict)
                    )
                else:
                    s1.append(fetchadd(cols[s1ind], strandDict))
                    s2.append(fetchadd(cols[s2ind], strandDict))
            N += 1
        if (not stripline) or (len(c1) == curMaxLen):
            res = dd.push(
                ar(c1, 32), ar(c2, 32), ar(p1, 32), ar(p2, 32), ar(s1, 32), ar(s2, 32)
            )
            if not stripline:
                res = np.concatenate([res, dd.finish()])

            for i in range(len(res)):
                # not duplicated pair:
                if not res[i]:
                    outstream.write(line_buffer[i])
                    # don't forget terminal newline
                    outstream.write("\n")
                    if out_stat:
                        out_stat.add_pair(
                            cols_buffer[i][c1ind],
                            int(cols_buffer[i][p1ind]),
                            cols_buffer[i][s1ind],
                            cols_buffer[i][c2ind],
                            int(cols_buffer[i][p2ind]),
                            cols_buffer[i][s2ind],
                            cols_buffer[i][ptind],
                        )
                # duplicated pair:
                else:
                    if out_stat:
                        out_stat.add_pair(
                            cols_buffer[i][c1ind],
                            int(cols_buffer[i][p1ind]),
                            cols_buffer[i][s1ind],
                            cols_buffer[i][c2ind],
                            int(cols_buffer[i][p2ind]),
                            cols_buffer[i][s2ind],
                            "DD",
                        )
                    if outstream_dups:
                        outstream_dups.write(
                            # DD-marked pair:
                            sep.join(mark_split_pair_as_dup(cols_buffer[i]))
                            if mark_dups
                            # pair as is:
                            else line_buffer[i]
                        )
                        # don't forget terminal newline
                        outstream_dups.write("\n")

            # flush buffers and perform necessary checks here:
            c1 = []
            c2 = []
            p1 = []
            p2 = []
            s1 = []
            s2 = []
            line_buffer = line_buffer[len(res) :]
            cols_buffer = cols_buffer[len(res) :]
            if not stripline:
                if len(line_buffer) != 0:
                    raise ValueError(
                        "{} lines left in the buffer, ".format(len(line_buffer))
                        + "should be none;"
                        + "something went terribly wrong"
                    )
                break
        # process next line ...
    # all lines have been processed at this point.
    # streaming_dedup is over.
    t1 = time.time()
    t = t1 - t0
    print(f"total time: {t}")
    print(f"time per mln pairs: {t/N*1e6}")


if __name__ == "__main__":
    dedup()
