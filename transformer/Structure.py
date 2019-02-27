import torch
import numpy as np
import torch.nn.functional as F
import Sidechains

def generate_coords(angles, pad_loc, input_seq, device):
    """ Given a tensor of angles (L x 11), produces the entire set of cartesian coordinates using the NeRF method,
        (L x A` x 3), where A` is the number of atoms generated (depends on amino acid sequence)."""
    bb_arr = init_backbone(angles, device)
    sc_arr = init_sidechain(angles, bb_arr)

    for i in range(1, pad_loc):
        bb_pts = extend_backbone(i, angles, bb_arr, device)
        bb_arr += bb_pts

        # Extend sidechain
        sc_pts = Sidechains.extend_sidechain(i, angles, bb_arr, sc_arr, input_seq)
        sc_arr += sc_pts


    return torch.stack(bb_arr + sc_arr)


def init_sidechain(angles, backbone):
    """ Builds the first sidechain based off of the first backbone atoms."""
    # TODO init sidechains
    return [torch.zeros(3)]


def init_backbone(angles, device):
    """ Given an angle matrix (RES x ANG), this initializes the first 3 backbone points (which are arbitrary) and
        returns a TensorArray of the size required to hold all the coordinates. """
    bondlens = {"n-ca": 1.442, "ca-c": 1.498, "c-n": 1.379}
    a1 = torch.zeros(3).to(device)

    if device.type == "cuda":
        a2 = a1 + torch.cuda.FloatTensor([bondlens["n-ca"], 0, 0])
        a3x = torch.cos(np.pi - angles[0, 3]) * bondlens["ca-c"]
        a3y = torch.sin(np.pi - angles[0, 3]) * bondlens['ca-c']
        a3 = torch.cuda.FloatTensor([a3x, a3y, 0])
    else:
        a2 = a1 + torch.FloatTensor([bondlens["n-ca"], 0, 0])
        a3x = torch.cos(np.pi - angles[0, 3]) * bondlens["ca-c"]
        a3y = torch.sin(np.pi - angles[0, 3]) * bondlens['ca-c']
        a3 = torch.FloatTensor([a3x, a3y, 0])



    starting_coords = [a1, a2, a3]

    return starting_coords


def extend_backbone(i, angles, coords, device):
    """ Returns backbone coordinates for the residue angles[pos]."""
    bondlens = {"n-ca": 1.442, "ca-c": 1.498, "c-n": 1.379}
    bb_pts = []
    for j in range(3):
        if j == 0:
            # we are placing N
            t = angles[i, 4]  # thetas["ca-c-n"]
            b = bondlens["c-n"]
            dihedral = angles[i - 1, 1]  # psi of previous residue
        elif j == 1:
            # we are placing Ca
            t = angles[i, 5]  # thetas["c-n-ca"]
            b = bondlens["n-ca"]
            dihedral = angles[i - 1, 2]  # omega of previous residue
        else:
            # we are placing C
            t = angles[i, 3]  # thetas["n-ca-c"]
            b = bondlens["ca-c"]
            dihedral = angles[i, 0]  # phi of current residue
        p3 = coords[-3]
        p2 = coords[-2]
        p1 = coords[-1]
        next_pt = nerf(p3, p2, p1, b, t, dihedral, device)
        bb_pts.append(next_pt)


    return bb_pts


def l2_normalize(t, device, eps=1e-12):
    """ Safe L2-normalization for pytorch."""
    epsilon = torch.FloatTensor([eps]).to(device)
    return t / torch.sqrt(torch.max((t**2).sum(), epsilon))


def nerf(a, b, c, l, theta, chi, device):
    '''
    Nerf method of finding 4th coord (d)
    in cartesian space
    Params:
    a, b, c : coords of 3 points
    l : bond length between c and d
    theta : bond angle between b, c, d (in degrees)
    chi : dihedral using a, b, c, d (in degrees)
    Returns:
    d : tuple of (x, y, z) in cartesian space
    '''
    # calculate unit vectors AB and BC

    W_hat = l2_normalize(b - a, device)
    x_hat = l2_normalize(c - b, device)

    # calculate unit normals n = AB x BC
    # and p = n x BC
    n_unit = torch.cross(W_hat, x_hat)
    z_hat = l2_normalize(n_unit, device)
    y_hat = torch.cross(z_hat, x_hat)

    # create rotation matrix [BC; p; n] (3x3)
    M = torch.stack([x_hat, y_hat, z_hat], dim=1)

    # calculate coord pre rotation matrix
    d = torch.stack([torch.squeeze(-l * torch.cos(theta)),
                     torch.squeeze(l * torch.sin(theta) * torch.cos(chi)),
                     torch.squeeze(l * torch.sin(theta) * torch.sin(chi))])

    # calculate with rotation as our final output

    d = d.unsqueeze(1)

    res = c + torch.mm(M, d).squeeze()

    return res.squeeze()
