import math
import numpy as np
import torch.nn.functional as F
import torch
from scipy.optimize import minimize
import scipy.optimize as optimize
from utils.ParamList import ParamList


def sigmoid_hm(hm_features):
    x = hm_features.sigmoid_()
    x = x.clamp(min=1e-4, max=1 - 1e-4)

    return x


def nms_hm(heat_map, kernel=3):
    pad = (kernel - 1) // 2

    hmax = F.max_pool2d(heat_map,
                        kernel_size=(kernel, kernel),
                        stride=1,
                        padding=pad)
    eq_index = (hmax == heat_map).float()

    return heat_map * eq_index


def gaussian2D(sigma, radius, size):
    '''

    :param sigma: shape(N,)
    :param radius: shape(N,)
    :param size: gaussian kernel size
    :return:
    '''
    device = sigma.device
    N = sigma.shape[0]

    offset_x = torch.arange(-size, size + 1, 1).to(device).type_as(sigma)
    offset_y = torch.arange(-size, size + 1, 1).to(device).type_as(sigma)
    offset_y, offset_x = torch.meshgrid([offset_x, offset_y])
    offset_xy = torch.cat([offset_x.contiguous().view(1, -1, 1), offset_y.contiguous().view(1, -1, 1)], dim=-1).repeat(
        N, 1, 1)  # N x M x 2
    gaussian_kernel = -1 * (offset_xy[..., 0] ** 2 + offset_xy[..., 1] ** 2) / (2 * (sigma.view(N, 1) ** 2))
    gaussian_kernel = torch.exp(gaussian_kernel)  # N x M
    reset = (offset_xy[..., 0] ** 2 + offset_xy[..., 1] ** 2) > radius.view(N, 1) ** 2
    gaussian_kernel[reset] = 0
    gaussian_kernel = gaussian_kernel.type_as(sigma)
    return gaussian_kernel, offset_xy


def fill_up_weights(up):
    # todo: we can replace math here?
    w = up.weight.data
    f = math.ceil(w.size(2) / 2)
    c = (2 * f - 1 - f % 2) / (2. * f)
    for i in range(w.size(2)):
        for j in range(w.size(3)):
            w[0, 0, i, j] = \
                (1 - math.fabs(i / f - c)) * (1 - math.fabs(j / f - c))
    for c in range(1, w.size(0)):
        w[c, 0, :, :] = w[0, 0, :, :]


def rotation_matrix(yaw, pitch=0, roll=0):
    tx = roll
    ty = yaw
    tz = pitch
    sin_ty = 0 if abs(np.sin(ty)) < 1e-3 else np.sin(ty)
    cos_ty = 0 if abs(np.cos(ty)) < 1e-3 else np.cos(ty)
    Rx = np.array([[1, 0, 0], [0, np.cos(tx), -np.sin(tx)], [0, np.sin(tx), np.cos(tx)]])
    Ry = np.array([[cos_ty, 0, sin_ty], [0, 1, 0], [-sin_ty, 0, cos_ty]])
    Rz = np.array([[np.cos(tz), -np.sin(tz), 0], [np.sin(tz), np.cos(tz), 0], [0, 0, 1]])

    return Ry.reshape([3, 3])


# option to rotate and shift (for label info)
def create_corners(dimension, location=None, R=None):
    dx = dimension[2] / 2  # L
    dy = dimension[0] / 2  # H
    dz = dimension[1] / 2  # W
    dim = np.diag([dx, dy, dz])
    x_corners = []
    y_corners = []
    z_corners = []
    #             / x
    #            /
    #    z -----
    #           |
    #           | y
    #          2----------3
    #         /|         /|
    #        / |        / |
    #       /  0-------/--1
    #      /  /       /  /
    #     6--/-------7  /
    #     | /        | /
    #     |/         |/
    #     4----------5
    for i in [1, -1]:  # x
        for j in [1, -1]:  # y
            for k in [1, -1]:  # z
                x_corners.append(i)
                y_corners.append(j)
                z_corners.append(k)

    x_corners.append(0)
    y_corners.append(0)
    z_corners.append(0)
    corners = np.vstack([x_corners, y_corners, z_corners])

    M = np.matmul(R, dim)
    corners = np.matmul(M, corners)
    location = np.array(location).reshape(3, 1)
    corners += location

    return corners


def create_birdview_corners(dimension, location=None, R=None):
    dx = dimension[2] / 2  # L
    dy = dimension[0] / 2  # H
    dz = dimension[1] / 2  # W
    dim = np.diag([dx, dy, dz])
    x_corners = []
    y_corners = []
    z_corners = []

    for i in [1, -1]:
        for k in [1, -1]:
            x_corners.append(i)
            y_corners.append(0)
            z_corners.append(k)

    corners = np.vstack([x_corners, y_corners, z_corners])

    M = np.matmul(R, dim)
    corners = np.matmul(M, corners)
    location = np.array(location).reshape(3, 1)
    corners += location

    return corners.T


def calc_proj_corners(dimension, location, Ry, K):
    R = rotation_matrix(Ry)
    corners_3d = create_corners(dimension, location, R)  # [3, N]
    proj_2d = np.matmul(K, corners_3d)
    proj_2d[:2, :] /= (proj_2d[None, 2, :] + 1e-6)
    return proj_2d[:2, :].T


def aimFun(*args):
    '''

    :param x: [sin(theta), cos(theta), l, h, w, x, y, z]
    :param args:
    :return:
    '''
    Cor, K, UV = args
    cost = 1e-4
    Cor = Cor.T
    UV = UV.T

    def fun(x):
        obj = 0
        for cor, uv in zip(Cor, UV):
            xc = cor[0] * x[2] * x[1] + cor[2] * x[4] * x[0] + x[5]
            yc = cor[1] * x[3] + x[6]
            zc = -cor[0] * x[2] * x[0] + cor[2] * x[4] * x[1] + x[7]
            obj += (xc * K[0, 0] / (zc + cost) + K[0, 2] - uv[0]) ** 2
            obj += (yc * K[1, 1] / (zc + cost) + K[1, 2] - uv[1]) ** 2
        return obj

    return fun


def aimFun1(*args):
    '''

    :param x: [sin(theta), cos(theta), l, h, w, x, y, z]
    :param args:
    :return:
    '''
    Cor, K, UV = args
    cost = 1e-4

    def fun(x):
        R = np.array([[x[1], 0, x[0]],
                      [0, 1, 0],
                      [-x[0], 0, x[1]]])
        T = np.array([[x[-3]],
                      [x[-2]],
                      [x[-1]]])
        D = np.diag([x[2], x[3], x[4]])
        vertexs = K @ (R @ D @ Cor + T)
        vertexs[:2, :] /= (vertexs[None, 2, :] + cost)
        err = np.sum((vertexs[:2, :] - UV) ** 2)
        return err

    return fun


def jac(*args):
    '''

    :param x: [sin(theta), cos(theta), l, h, w, x, y, z]
    :param args:
    :return:
    '''
    Cor, K, UV = args
    cost = 1e-6
    Cor = Cor.T
    UV = UV.T

    def fun(x):
        err = np.zeros((len(x),), dtype=np.float64)
        for cor, uv in zip(Cor, UV):
            xc = cor[0] * x[2] * x[1] + cor[2] * x[4] * x[0] + x[5]
            yc = cor[1] * x[3] + x[6]
            zc = -cor[0] * x[2] * x[0] + cor[2] * x[4] * x[1] + x[7]
            deriv_ex = (xc * K[0, 0] / (zc + cost) + K[0, 2] - uv[0]) * 2
            deriv_ey = (yc * K[1, 1] / (zc + cost) + K[1, 2] - uv[1]) * 2
            deriv_ex_x = np.array([cor[2] * x[4], cor[0] * x[2], cor[0] * x[1], 0, cor[2] * x[0], 1, 0, 0])
            deriv_ex_y = np.array([0, 0, 0, cor[1], 0, 0, 1, 0])
            deriv_ex_z = np.array([-cor[0] * x[2], cor[2] * x[4], -cor[0] * x[0], 0, cor[2] * x[1], 0, 0, 1])
            deriv_ex_x = K[0, 0] * (deriv_ex_x * zc - deriv_ex_z * xc) / (zc ** 2 + cost)
            deriv_ex_y = K[1, 1] * (deriv_ex_y * zc - deriv_ex_z * yc) / (zc ** 2 + cost)
            err += deriv_ex * deriv_ex_x + deriv_ey * deriv_ex_y
        return err

    return fun

def aimfun2(*args):
    '''

    :param x: [sin(theta), cos(theta), l, h, w, x, y, z]
    :param args:
    :return:
    '''
    f = aimFun(*args)
    g = jac(*args)

    return lambda x: (f(x), g(x))

def constraint():
    min_v = 1e-10
    max_v = 1e1
    cons = ({'type': 'eq', 'fun': lambda x: x[0] ** 2 + x[1] ** 2 - 1},
            {'type': 'ineq', 'fun': lambda x: x[2] - min_v},
            {'type': 'ineq', 'fun': lambda x: x[3] - min_v},
            {'type': 'ineq', 'fun': lambda x: x[4] - min_v},
            {'type': 'ineq', 'fun': lambda x: -x[2] + max_v},
            {'type': 'ineq', 'fun': lambda x: -x[3] + max_v},
            {'type': 'ineq', 'fun': lambda x: -x[4] + max_v},
            {'type': 'ineq', 'fun': lambda x: -x[6]},
            {'type': 'ineq', 'fun': lambda x: x[6] + max_v / 2},
            {'type': 'ineq', 'fun': lambda x: x[7]})
    return cons


def optim_decode_bbox3d(clses, bbox3d_projs, K, ref_dim, ref_loc):
    '''

    :param clses: (N, )
    :param bbox3d_projs: (N, 8, 2)
    :return:
    '''

    x_corners = []
    y_corners = []
    z_corners = []
    for i in [1, -1]:  # x
        for j in [1, -1]:  # y
            for k in [1, -1]:  # z
                x_corners.append(i)
                y_corners.append(j)
                z_corners.append(k)
    Cor = np.vstack([x_corners, y_corners, z_corners]) * 0.5
    K = K.reshape(3, 3)
    cons = constraint()
    dims = []
    Rys = []
    locs = []
    clses_new = []
    Ks = []

    options = {'disp': None, 'maxcor': 10, 'ftol': 2.220446049250313e-09, 'gtol': 1e-05, 'eps': 1e-08,
               'maxfun': 15000, 'maxiter': 15000, 'iprint': -1, 'maxls': 20, 'finite_diff_rel_step': None}
    for cls, UV in zip(clses, bbox3d_projs):
        dim = ref_dim[cls]
        X0 = np.array([0, 1] + [dim[2], dim[0], dim[1]] + ref_loc)
        res = minimize(aimFun(*(Cor, K, UV.T)), X0, method='L-BFGS-B',
                       jac=jac(*(Cor, K, UV.T)), constraints=cons, options=options)

        if res.fun < 0.1:
            x = res.x
            Ry = np.arctan2(x[0], x[1])
            Rys.append(Ry)
            dims.append(np.array([x[3], x[4], x[2]]).reshape(1, 3))
            locs.append(np.array([x[-3], x[-2], x[-1]]).reshape(1, 3))
            clses_new.append(cls)
            Ks.append(K.reshape(1, 9))
    out = ParamList((640, 640))
    out.add_field('class', clses_new)
    out.add_field('Ry', np.array(Rys))
    out.add_field('dimension', np.concatenate(dims, axis=0) if len(dims) else np.zeros((0, 3)))
    out.add_field('location', np.concatenate(locs, axis=0) if len(dims) else np.zeros((0, 3)))
    out.add_field('K', np.concatenate(Ks, axis=0) if len(dims) else np.zeros((0, 9)))
    return out
