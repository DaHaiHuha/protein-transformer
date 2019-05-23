import argparse
import datetime
import multiprocessing
import pickle
import sys
from multiprocessing import Pool

import numpy as np
import prody as pr
import requests
import torch
import tqdm
from sklearn.model_selection import train_test_split

sys.path.extend("../transformer/")
from transformer.Sidechains import SC_DATA, NUM_PREDICTED_ANGLES
from transformer.Structure import FICTITIOUS_C
pr.confProDy(verbosity='error')


# Set amino acid encoding and angle downloading methods
def angle_list_to_sin_cos(angs, reshape=True):
    """ Given a list of angles, returns a new list where those angles have
        been turned into their sines and cosines. If reshape is False, a new dim.
        is added that can hold the sine and cosine of each angle,
        i.e. (len x #angs) -> (len x #angs x 2). If reshape is true, this last
        dim. is squashed so that the list of angles becomes
        [cos sin cos sin ...]. """
    new_list = []
    new_pad_char = np.array([1, 0])
    for a in angs:
        new_mat = np.zeros((a.shape[0], a.shape[1], 2))
        new_mat[:, :, 0] = np.cos(a)
        new_mat[:, :, 1] = np.sin(a)
        #         new_mat = (new_mat != new_pad_char) * new_mat
        if reshape:
            new_list.append(new_mat.reshape(-1, NUM_PREDICTED_ANGLES * 2))
        else:
            new_list.append(new_mat)
    return new_list


def seq_to_onehot(seq):
    """ Given an AA sequence, returns a vector of one-hot vectors."""
    vector_array = []
    for aa in seq:
        one_hot = np.zeros(len(AA_MAP), dtype=bool)
        one_hot[AA_MAP[aa]] = 1
        vector_array.append(one_hot)
    return np.asarray(vector_array)


# get bond angles
def get_bond_angles(res, next_res):
    """ Given 2 residues, returns the ncac, cacn, and cnca bond angles between them."""
    atoms = res.backbone.copy()
    atoms_next = next_res.backbone.copy()
    ncac = pr.calcAngle(atoms[0], atoms[1], atoms[2], radian=True)
    cacn = pr.calcAngle(atoms[1], atoms[2], atoms_next[0], radian=True)
    cnca = pr.calcAngle(atoms[2], atoms_next[0], atoms_next[1], radian=True)
    return ncac, cacn, cnca


def measure_bond_angles(residue, res_idx, all_res):
    """ Given a residue, measure the ncac, cacn, and cnca bond angles. """
    if res_idx == len(all_res) - 1:
        bondangles = [0, 0, 0]
    else:
        bondangles = list(get_bond_angles(residue, all_res[res_idx + 1]))
    return bondangles


def measure_phi_psi_omega(residue, outofboundchar=0):
    """ Returns phi, psi, omega for a residue, replacing out-of-bounds angles with outofboundchar."""
    try:
        phi = pr.calcPhi(residue, radian=True, dist=None)
    except ValueError:
        phi = outofboundchar
    try:
        psi = pr.calcPsi(residue, radian=True, dist=None)
    except ValueError:
        psi = outofboundchar
    try:
        omega = pr.calcOmega(residue, radian=True, dist=None)
    except ValueError:
        omega = outofboundchar
    return [phi, psi, omega]


def compute_single_dihedral(atoms):
    """ Given an iterable of 4 Atoms, uses Prody to calculate the dihedral angle between them in radians. """
    return pr.calcDihedral(atoms[0], atoms[1], atoms[2], atoms[3], radian=True)[0]


def getDihedral(coords1, coords2, coords3, coords4, radian=False):
    """ Returns the dihedral angle in degrees. Modified from prody.measure.measure to use a numerically safe
        normalization method. """
    rad2deg = 180 / np.pi
    eps = 1e-6

    a1 = coords2 - coords1
    a2 = coords3 - coords2
    a3 = coords4 - coords3

    v1 = np.cross(a1, a2)
    v1 = v1 / (v1 * v1).sum(-1) ** 0.5
    v2 = np.cross(a2, a3)
    v2 = v2 / (v2 * v2).sum(-1) ** 0.5
    porm = np.sign((v1 * a3).sum(-1))
    arccos_input_raw = (v1 * v2).sum(-1) / ((v1 ** 2).sum(-1) * (v2 ** 2).sum(-1)) ** 0.5
    if -1 <= arccos_input_raw <= 1:
        arccos_input = arccos_input_raw
    elif arccos_input_raw > 1 and arccos_input_raw - 1 < eps:
        arccos_input = 1
    elif arccos_input_raw < -1 and np.abs(arccos_input_raw) - 1 < eps:
        arccos_input = -1
    else:
        raise ArithmeticError("Numerical issue with input to arccos.")
    rad = np.arccos(arccos_input)
    if not porm == 0:
        rad = rad * porm
    if radian:
        return rad
    else:
        return rad * rad2deg


def check_standard_continuous(residue, prev_res_num):
    """ Asserts that the residue is standard and that the chain is continuous. """
    if not residue.isstdaa:
        if args.debug: print("Found a non-std AA. Why didn't you catch this? " + str(residue.getNames()))
        return False
    if residue.getResnum() != prev_res_num:
        if args.debug: print("Chain is non-continuous")
        return False
    return True


def get_fictitious_c(first_3_atoms):
    """ Given the first three backbone atoms of a protein, aligns the starting atom positions used when generating
        the structure later (starting at the origin). The main benefit of this is that we can construct a fictitious
        Carbon atom that lies before the first Nitrogen atom. This atom is used to measure the [C-1, N, CA, CB]
        dihedral at the time of data aquisition, and is later used to correctly place the first CB when generating
        the structure later. This function returns the coordinates of this 'fictitious C'. """
    mobile_atom_group_complete = [0 for _ in range(4)]
    mobile_atom_group_complete[0] = np.array(FICTITIOUS_C)
    mobile_atom_group_complete[1] = np.array([0., 0., 0.])
    mobile_atom_group_complete[2] = np.array([1.4420, 0, 0])
    mobile_atom_group_complete[3] = np.array([2.0080, 1.3870, 0])
    mobile_atom_group_complete = np.array(mobile_atom_group_complete)
    mobile_atom_group_alignment = mobile_atom_group_complete[1:]

    target_atom_group = np.array([a.getCoords() for a in first_3_atoms]).reshape(3, 3)
    t = pr.calcTransformation(mobile_atom_group_alignment, target_atom_group)
    mobile_atom_group_aligned = t.apply(mobile_atom_group_complete)
    return mobile_atom_group_aligned[0]


def compute_all_res_dihedrals(atom_names, residue, prev_residue, backbone, bondangles, pad_char=0):
    """ Computes all angles to predict for a given residue. If the residue is the first in the protein chain,
        a fictitious C atom is placed before the first N. This is used to compute a [C-1, N, CA, CB] dihedral
        angle. If it is not the first residue in the chain, the previous residue's C is used instead.
        Then, each group of 4 atoms in atom_names is used to generate a list of dihedral angles for this
        residue. """
    res_dihedrals = []
    if len(atom_names) > 0:
        if prev_residue is None:
            atoms = [residue.select("name " + an) for an in atom_names]
            if None in atoms:
                return None
            previous_c = get_fictitious_c(atoms[:3])
            res_dihedrals = [getDihedral(previous_c, atoms[0].getCoords()[0], atoms[1].getCoords()[0],
                                         atoms[2].getCoords()[0], radian=True)]
        elif prev_residue is not None:
            atoms = [prev_residue.select("name C")] + [residue.select("name " + an) for an in atom_names]
            if None in atoms:
                return None
        for n in range(len(atoms) - 3):
            dihe_atoms = atoms[n:n + 4]
            res_dihedrals.append(compute_single_dihedral(dihe_atoms))

    return backbone + bondangles + res_dihedrals + (NUM_PREDICTED_ANGLES - 6 - len(res_dihedrals)) * [pad_char]


# get angles from chain
def get_angles_from_chain(chain, pdb_id):
    """ Given a ProDy Chain object (from a Hierarchical View), return a numpy array of
        angles. Returns None if the PDB should be ignored due to weird artifacts. Also measures
        the bond angles along the peptide backbone, since they account for significat variation.
        i.e. [[phi, psi, omega, ncac, cacn, cnca, chi1, chi2, chi3, chi4, chi5], [...] ...] """

    dihedrals = []
    try:
        if chain.nonstdaa:
            if args.debug: print("Non-standard AAs found.")
            return None
        sequence = chain.getSequence()
        chain = chain.select("protein and not hetero").copy()
    except Exception as e:
        if args.debug: print("Problem loading sequence.", e)
        return None

    all_residues = list(chain.iterResidues())
    prev = all_residues[0].getResnum()
    prev_res = None
    for res_id, res in enumerate(all_residues):
        if not check_standard_continuous(res, prev):
            return None
        else:
            prev = res.getResnum() + 1

        res_backbone = measure_phi_psi_omega(res)
        res_bond_angles = measure_bond_angles(res, res_id, all_residues)

        atom_names = ["N", "CA"]
        # Special cases
        # TODO verify correctness of GLY, PRO atom_names
        if res.getResname() in ["GLY", "PRO"]:
            atom_names = SC_DATA[res.getResname()]["predicted"]
        else:
            atom_names += SC_DATA[res.getResname()]["predicted"]

        calculated_dihedrals = compute_all_res_dihedrals(atom_names, res, prev_res, res_backbone, res_bond_angles)
        if calculated_dihedrals is None:
            return None
        dihedrals.append(calculated_dihedrals)
        prev_res = res

    dihedrals_np = np.asarray(dihedrals)
    # Check for NaNs - they shouldn't be here, but certainly should be excluded if they are.
    if np.any(np.isnan(dihedrals_np)):
        if args.debug: print("NaNs found")
        return None
    return dihedrals_np, sequence


# 3b. Parallelized method of downloading data
def work(pdb_id):
    pdb_dihedrals = []
    pdb_sequences = []
    ids = []

    try:
        pdb = pdb_id.split(":")
        pdb_id = pdb[0]
        pdb_hv = pr.parsePDB(pdb_id).getHierView()
        # if less than 2 chains,  continue
        numChains = pdb_hv.numChains()
        if args.single_chain_only and numChains > 1:
            return None

        prevchainseq = None
        for chain in pdb_hv:
            if prevchainseq is None:
                prevchainseq = chain.getSequence()
            elif chain.getSequence() == prevchainseq:  # chain sequences are identical
                if args.debug: print("identical chain found")
                continue
            else:
                if args.debug: print("Num Chains > 1 & seq not identical, returning None for: ", pdb_id)
                return None
            chain_id = chain.getChid()
            dihedrals_sequence = get_angles_from_chain(chain, pdb_id)
            if dihedrals_sequence is None:
                continue
            dihedrals, sequence = dihedrals_sequence
            pdb_dihedrals.append(dihedrals)
            pdb_sequences.append(sequence)
            ids.append(pdb_id + "_" + chain_id)

    except Exception as e:
        # print("Whoops, returning where I am.", e)
        raise e
    if len(pdb_dihedrals) == 0:
        return None
    else:
        return pdb_dihedrals, pdb_sequences, ids


# function for additional checks of matrices
def additional_checks(matrix):
    zeros = not np.any(matrix)
    if not np.any(np.isnan(matrix)) and not np.any(np.isinf(matrix)) and not zeros:
        return True
    else:
        if args.debug: print("additional checks not passed")


def load_query(fname):
    # obtain query from file
    with open(fname, "r") as qf:
        desc = qf.readline()
        query = qf.read()
    return query, desc


def download_pdbs_from_query(query):
    url = 'http://www.rcsb.org/pdb/rest/search'
    header = {'Content-Type': 'application/x-www-form-urlencoded'}
    response = requests.post(url, data=query, headers=header)
    if response.status_code != 200:
        if args.debug: print("Failed to retrieve results.")

    PDB_IDS = response.text.split("\n")
    PDB_IDS = list(filter(lambda x: x != "", PDB_IDS))
    print("Retrieved {0} PDB IDs.".format(len(PDB_IDS)))
    return PDB_IDS


if __name__ == "__main__":
    global args, desc
    parser = argparse.ArgumentParser(description="Searches through a query of PDBs and parses/downloads chains")
    parser.add_argument('query_file', type=str, help='Path to query file')
    parser.add_argument('-o', '--out_file', type=str, help='Path to output file (.pkl file)')
    parser.add_argument('-sc', '--single_chain_only', action="store_true", help='Only keep PDBs with a single chain.')
    parser.add_argument('-d', '--debug', action="store_true", help='Print debug print statements.')
    parser.add_argument("--pdb_dir", default="/home/jok120/pdb/", type=str, help="Path for ProDy-downloaded PDB files.")
    parser.add_argument("-p", "--pickle", action="store_true",
                        help="Save data as a pickled dictionary instead of a torch-dictionary.")
    args = parser.parse_args()

    # Set up
    AA_MAP = {'A': 15, 'C': 0, 'D': 1, 'E': 17, 'F': 8, 'G': 10, 'H': 11, 'I': 5, 'K': 4, 'L': 12, 'M': 19, 'N': 9,
              'P': 6, 'Q': 3, 'R': 13, 'S': 2, 'T': 7, 'V': 16, 'W': 14, 'Y': 18}
    pr.pathPDBFolder(args.pdb_dir)
    np.set_printoptions(suppress=True)  # suppresses scientific notation when printing
    np.set_printoptions(threshold=np.nan)  # suppresses '...' when printing
    today = datetime.datetime.today()
    suffix = today.strftime("%y%m%d")
    if not args.out_file and args.pickle:
        args.out_file = "../data/data_" + suffix + ".pkl"
    elif not args.out_file and not args.pickle:
        args.out_file = "../data/data_" + suffix + ".tch"

    # Load query
    query, query_description = load_query(args.query_file)
    print("Description:", query_description)

    # Download PDB_IDS associated with query
    PDB_IDS = download_pdbs_from_query(query)

    # 3a. Iterate through all chains in PDB_IDs, saving all results to disk
    # Remove empty string PDB ids
    with Pool(multiprocessing.cpu_count()) as p:
        results = list(tqdm.tqdm(p.imap(work, PDB_IDS), total=len(PDB_IDS)))

    # 4. Throw out results that are None; unpack results with multiple chains
    MAX_LEN = 500
    results_onehots = []
    c = 0
    for r in results:
        if not r:
            # PDB failed to download
            continue
        ang, seq, i = r
        if len(seq[0]) > MAX_LEN:
            continue
        for j in range(len(ang)):
            results_onehots.append((ang[j], seq_to_onehot(seq[j]), i[j]))
            c += 1
    print(c, "chains successfully parsed and downloaded.")

    # 5a. Remove all one-hot (oh) vectors, angles, and sequence ids from tuples
    all_ohs = []
    all_angs = []
    all_ids = []
    for r in results_onehots:
        a, oh, i = r
        if additional_checks(oh) and additional_checks(a):
            all_ohs.append(oh)
            all_angs.append(a)
            all_ids.append(i)
    ohs_ids = list(zip(all_ohs, all_ids))

    # 5b. Split into train, test and validation sets. Report sizes.
    X_train, X_test, y_train, y_test = train_test_split(ohs_ids, all_angs, test_size=0.20, random_state=42)
    X_train, X_val, y_train, y_val = train_test_split(X_train, y_train, test_size=0.20, random_state=42)
    print("Train, test, validation set sizes:\n" + str(list(map(len, [X_train, X_test, X_val]))))

    # 5c. Separate PDB ID/Sequence tuples.
    X_train_labels = [x[1] for x in X_train]
    X_test_labels = [x[1] for x in X_test]
    X_val_labels = [x[1] for x in X_val]
    X_train = [x[0] for x in X_train]
    X_test = [x[0] for x in X_test]
    X_val = [x[0] for x in X_val]

    # 6. Create a dictionary data structure, using the sin/cos transformed angles
    date = datetime.datetime.now().strftime("%I:%M%p on %B %d, %Y")
    data = {"train": {"seq": X_train,
                      "ang": angle_list_to_sin_cos(y_train),
                      "ids": X_train_labels},
            "valid": {"seq": X_val,
                      "ang": angle_list_to_sin_cos(y_val),
                      "ids": X_val_labels},
            "test": {"seq": X_test,
                     "ang": angle_list_to_sin_cos(y_test),
                     "ids": X_test_labels},
            "settings": {"max_len": max(map(len, all_ohs))},
            "description": {query_description},
            "query": query,
            "date": {date}}
    # To parse date later, use datetime.datetime.strptime(date, "%I:%M%p on %B %d, %Y")

    # dump data
    if args.pickle:
        with open(args.out_file, "wb") as f:
            pickle.dump(data, f)
    else:
        torch.save(data, args.out_file)
