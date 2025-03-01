import numpy as np
import pandas as pd

import scipy.spatial
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from . import dedup_cython, pairsam_format
from .stats import PairCounter

from .._logging import get_logger

logger = get_logger()
import time

# Ignore pandas future warnings:
import warnings
warnings.simplefilter(action='ignore', category=FutureWarning)

# Setting for cython deduplication:
# you don't need to load more than 10k lines at a time b/c you get out of the
# CPU cache, so this parameter is not adjustable
MAX_LEN = 10000


def streaming_dedup(
    in_stream,
    colnames,
    chunksize,
    carryover,
    method,
    mark_dups,
    max_mismatch,
    extra_col_pairs,
    unmapped_chrom,
    outstream,
    outstream_dups,
    outstream_unmapped,
    keep_parent_id,
    out_stat,
    backend,
    n_proc,
):

    deduped_chunks = _dedup_stream(
        in_stream=in_stream,
        colnames=colnames,
        method=method,
        chunksize=chunksize,
        carryover=carryover,
        mark_dups=mark_dups,
        max_mismatch=max_mismatch,
        extra_col_pairs=extra_col_pairs,
        keep_parent_id=keep_parent_id,
        backend=backend,
        n_proc=n_proc,
    )

    t0 = time.time()
    N = 0

    for df_chunk in deduped_chunks:
        N += df_chunk.shape[0]

        # Write the stats if requested:
        if out_stat is not None:
            out_stat.add_pairs_from_dataframe(df_chunk, unmapped_chrom=unmapped_chrom)

        # Define masks of unmapped and duplicated reads:
        mask_mapped = np.logical_and(
            (df_chunk["chrom1"] != unmapped_chrom),
            (df_chunk["chrom2"] != unmapped_chrom),
        )
        mask_duplicates = df_chunk["duplicate"]

        # Clean up dataframe:
        df_chunk = df_chunk.drop(columns=["duplicate"])

        # Stream the dups:
        if outstream_dups:
            df_chunk.loc[mask_mapped & mask_duplicates, :].to_csv(
                outstream_dups, index=False, header=False, sep="\t"
            )

        # Drop readID if it was created (not needed for nodup and unmapped pairs):
        if keep_parent_id:
            df_chunk = df_chunk.drop(columns=["parent_readID"])

        # Stream unmapped:
        if outstream_unmapped:
            df_chunk.loc[~mask_mapped, :].to_csv(
                outstream_unmapped, index=False, header=False, sep="\t"
            )

        # Stream unique pairs:
        df_chunk.loc[mask_mapped & (~mask_duplicates), :].to_csv(
            outstream, index=False, header=False, sep="\t"
        )

    t1 = time.time()
    t = t1 - t0
    logger.debug(f"total time: {t}")
    if N > 0:
        logger.debug(f"time per mln pairs: {t/N*1e6}")
    else:
        logger.debug(f"Processed {N} pairs")


def _dedup_stream(
    in_stream,
    colnames,
    method,
    chunksize,
    carryover,
    mark_dups,
    max_mismatch,
    extra_col_pairs,
    keep_parent_id,
    backend,
    n_proc,
):
    # Stream the input dataframe:
    dfs = pd.read_table(
        in_stream, comment=None, names=colnames, chunksize=chunksize
    )

    # Set up the carryover dataframe:
    df_prev_nodups = pd.DataFrame([])
    prev_i = 0

    # Iterate over chunks:
    for df in dfs:
        df_marked = _dedup_chunk(
            pd.concat([df_prev_nodups, df], axis=0, ignore_index=True).reset_index(
                drop=True
            ),
            r=max_mismatch,
            method=method,
            keep_parent_id=keep_parent_id,
            extra_col_pairs=extra_col_pairs,
            backend=backend,
            n_proc=n_proc,
        )
        df_marked = df_marked.loc[prev_i:, :].reset_index(drop=True)
        mask_duplicated = df_marked["duplicate"]
        if mark_dups:
            df_marked.loc[mask_duplicated, "pair_type"] = "DD"

        # Filter out duplicates and store specific columns:
        df_nodups = df_marked.loc[~mask_duplicated, colnames]

        # Re-define carryover pairs:
        df_prev_nodups = df_nodups.tail(carryover).reset_index(drop=True)
        prev_i = len(df_prev_nodups)

        yield df_marked


def _dedup_chunk(
    df,
    r,
    method,
    keep_parent_id,
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
    keep_parent_id : bool
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

    # Store the index of the dataframe:
    index_colname = df.index.name
    if index_colname is None:
        index_colname = "index"
    df = df.reset_index()  # Remove the index temporarily

    # Set up columns to store the dedup info:
    df.loc[:, "clusterid"] = np.nan
    df.loc[:, "duplicate"] = False

    # Split mapped and unmapped reads:
    mask_unmapped = (df["chrom1"] == unmapped_chrom) | (df["chrom2"] == unmapped_chrom)
    df_unmapped = df.loc[mask_unmapped, :].copy()
    df_mapped = df.loc[~mask_unmapped, :].copy()
    N_mapped = df_mapped.shape[0]

    # If there are some mapped reads, dedup them:
    if N_mapped > 0:
        if backend == "sklearn":
            a = neighbors.radius_neighbors_graph(
                df_mapped[["pos1", "pos2"]],
                radius=r,
                p=p,
                n_jobs=n_proc,
            )
            a0, a1 = a.nonzero()
        elif backend == "scipy":
            z = scipy.spatial.cKDTree(df_mapped[["pos1", "pos2"]])
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
                df_mapped.iloc[a0, df_mapped.columns.get_loc(lc)].values
                == df_mapped.iloc[a1, df_mapped.columns.get_loc(rc)].values
                for (lc, rc) in need_to_match
            ],
            axis=0,
        )
        a0 = a0[nonpos_matches]
        a1 = a1[nonpos_matches]
        a_mat = coo_matrix((np.ones_like(a0), (a0, a1)), shape=(N_mapped, N_mapped))

        # Set up inferred clusterIDs:
        df_mapped.loc[:, "clusterid"] = connected_components(a_mat, directed=False)[1]

    mask_dups = df_mapped["clusterid"].duplicated()
    df_mapped.loc[mask_dups, "duplicate"] = True

    # Mark parent IDs if requested:
    if keep_parent_id:
        df_mapped.loc[:, "parent_readID"] = df_mapped["clusterid"].map(
            df_mapped[~mask_dups].set_index("clusterid")["readID"]
        )
        df_unmapped.loc[:, "parent_readID"] = ""

    # Reconstruct original dataframe with removed duplicated reads:
    df = pd.concat([df_unmapped, df_mapped]).reset_index(drop=True)
    df = df.set_index(index_colname)  # Set up the original index
    df = df.drop(
        ["clusterid"], axis=1
    )  # Remove the information that we don't need anymore:

    return df


### Cython deduplication ####
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
    keep_parent_id=False,
    readid_ind=0,
):
    """
    Cython-powered deduplication with online algorithm based on indexed list.

    Parameters
    ----------
    method: "max" or "sum"
    max_mismatch: maximum allowed mismatch to count the pairs as duplicates
    sep: separator of the fields in the input file
    c1ind: index of the chr1 column
    c2ind: index of the chr2 column
    p1ind: index of the pos1 column
    p2ind: index of the pos2 column
    s1ind: index of the strand1 column
    s2ind: index of the strand2 column
    extra_cols1: extra columns for left alignment in a pair to add
    extra_cols2: extra columns for right alignment in a pair to add
    unmapped_chrom: Symbol of the chromosome for the unmapped alignment
    instream: input stream of pair file
    outstream: output stram of deduplicated pairs
    outstream_dups: output stream of duplicates (optionally with added parent_id, see keep_parent_id option)
    outstream_unmapped: output stram of unmapped pairs
    out_stat: output statistics
    mark_dups: if True, will add "DD" as the pair_type
    keep_parent_id: if True, additional column "parent_readID will be added to the output, can be useful for optical duplicates search
    readid_ind: index of the readID column in the input file

    Returns
    -------

    """
    maxind = max(c1ind, c2ind, p1ind, p2ind, s1ind, s2ind)
    if bool(extra_cols1) and bool(extra_cols2):
        maxind = max(maxind, max(extra_cols1), max(extra_cols2))

    all_scols1 = [s1ind] + extra_cols1
    all_scols2 = [s2ind] + extra_cols2

    # if we do stats in the dedup, we need PAIR_TYPE
    # i do not see way around this:
    if out_stat:
        ptind = pairsam_format.COL_PTYPE
        maxind = max(maxind, ptind)

    dd = dedup_cython.OnlineDuplicateDetector(
        method, max_mismatch, returnData=False, keep_parent_id=keep_parent_id
    )

    c1 = []
    c2 = []
    p1 = []
    p2 = []
    s1 = []
    s2 = []
    idx = []
    line_buffer = []
    cols_buffer = []
    chromDict = {}
    strandDict = {}
    curMaxLen = max(MAX_LEN, dd.getLen())

    t0 = time.time()
    N = 0

    instream = iter(instream)
    read_idx = 0  # read index to mark the parent readID
    while True:
        rawline = next(instream, None)
        stripline = rawline.strip('\n') if rawline else None

        # take care of empty lines not at the end of the file separately
        if rawline and (not stripline):
            logger.warning("Empty line detected not at the end of the file")
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

                idx.append(read_idx)
                read_idx += 1

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
            if keep_parent_id:
                res, parents = dd.push(
                    ar(c1, 32),
                    ar(c2, 32),
                    ar(p1, 32),
                    ar(p2, 32),
                    ar(s1, 32),
                    ar(s2, 32),
                )

            else:
                res = dd.push(
                    ar(c1, 32),
                    ar(c2, 32),
                    ar(p1, 32),
                    ar(p2, 32),
                    ar(s1, 32),
                    ar(s2, 32),
                )

            if not stripline:
                if keep_parent_id:
                    res_tmp, parents_tmp = dd.finish()
                    parents = np.concatenate([parents, parents_tmp])

                else:
                    res_tmp = dd.finish()
                res = np.concatenate([res, res_tmp])

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
                        if mark_dups:  # DD-marked pair:
                            output = sep.join(mark_split_pair_as_dup(cols_buffer[i]))
                        else:  # pair as is:
                            output = line_buffer[i]

                        if keep_parent_id:  # Add parentID as the last column:
                            parent_readID = line_buffer[parents[i]].split(sep)[
                                readid_ind
                            ]
                            output = sep.join([output, parent_readID])

                        outstream_dups.write(output)

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
    logger.debug(f"total time: {t}")
    if N > 0:
        logger.debug(f"time per mln pairs: {t/N*1e6}")
    else:
        logger.debug(f"Processed {N} pairs")


def fetchadd(key, mydict):
    key = key.strip()
    if key not in mydict:
        mydict[key] = len(mydict)
    return mydict[key]


def ar(mylist, val):
    return np.array(mylist, dtype={8: np.int8, 16: np.int16, 32: np.int32}[val])


#### Markasdup utilities: ####
def mark_split_pair_as_dup(cols):
    # if the original columns ended with a new line, the marked columns
    # should as well.
    original_has_newline = cols[-1].endswith("\n")

    cols[pairsam_format.COL_PTYPE] = "DD"

    if (len(cols) > pairsam_format.COL_SAM1) and (len(cols) > pairsam_format.COL_SAM2):
        for i in (pairsam_format.COL_SAM1, pairsam_format.COL_SAM2):

            # split each sam column into sam entries, tag and assemble back
            cols[i] = pairsam_format.INTER_SAM_SEP.join(
                [
                    mark_sam_as_dup(sam)
                    for sam in cols[i].split(pairsam_format.INTER_SAM_SEP)
                ]
            )

    if original_has_newline and not cols[-1].endswith("\n"):
        cols[-1] = cols[-1] + "\n"
    return cols


def mark_sam_as_dup(sam):
    """Tag the binary flag and the optional pair type field of a sam entry
    as a PCR duplicate."""
    samcols = sam.split(pairsam_format.SAM_SEP)

    if len(samcols) == 1:
        return sam

    samcols[1] = str(int(samcols[1]) | 1024)

    for j in range(11, len(samcols)):
        if samcols[j].startswith("Yt:Z:"):
            samcols[j] = "Yt:Z:DD"
    return pairsam_format.SAM_SEP.join(samcols)
