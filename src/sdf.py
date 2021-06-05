import torch
import torch.nn as nn
import torch.nn.functional as F
import random

from .nerf import ( CommonNeRF, compute_pts_ts )
from .neural_blocks import ( SkipConnMLP )
from .utils import ( autograd, eikonal_loss )

# Use loss from IDR, taking max occupancy along NeRF as segmentation.
def sigmoid_loss(min_along_ray, densities, alpha: int=500):
  throughput = -min_along_ray.squeeze(-1) * alpha
  hits = (throughput > 0) & (densities > 0.5)
  misses = ~hits
  loss = 0
  if misses.any():
    loss = F.binary_cross_entropy_with_logits(
      throughput[misses].reshape(-1, 1),
      densities[misses].reshape(-1, 1),
      # weight points which are far from the rounded value lower
      #2 * (0.5 - (densities.round() - densities).abs()),
    )
    assert(loss.isfinite().all())
  return loss

class SDF(nn.Module):
  def __init__(
    self,
  ):
    super().__init__()
    self.mlp = SkipConnMLP(
      in_size = 3, out = 1, num_layers=6, hidden_size=64,
      activation=nn.Softplus(), last_layer_act=True,
    )
    self.values = None
    self.normals = None
  def forward(self, pts):
    with torch.enable_grad():
      if not pts.requires_grad: autograd_pts = pts.requires_grad_()
      else: autograd_pts = pts
      self.values = self.mlp(autograd_pts)
      self.normals = autograd(autograd_pts, self.values)
      assert(self.normals.isfinite().all())

      return self.values
  # performs a spherical march of an SDF, for a fixed number of iterations
  def sphere_march(
    self,
    r_o, r_d,
    iters: int = 64,
    eps: float = 1e-4,
    near: float = 0, far: float = 1,
  ) -> "IntersectionPoints":

    device = r_o.device
    hits = torch.zeros(r_o.shape[:-1] + (1,), dtype=torch.bool, device=device)
    curr_dist = torch.full_like(hits, near, dtype=torch.float)
    for i in range(iters):
      curr = r_o + r_d * curr_dist
      dist = self.mlp(curr)
      hits = hits | ((dist < eps) & (curr_dist >= near) & (curr_dist <= far))
      curr_dist = torch.where(~hits, curr_dist + dist, curr_dist)
      if hits.all(): break

    curr = r_o + r_d * curr_dist
    return curr, hits, curr_dist

class SDFNeRF(nn.Module):
  def __init__(
    self,
    nerf: CommonNeRF,
    sdf: SDF,
  ):
    super().__init__()
    self.nerf = nerf
    self.sdf = sdf
    self.min_along_rays = None
  def forward(self, rays):
    pts, ts, r_o, r_d = compute_pts_ts(rays, self.nerf.t_near, self.nerf.t_far, self.nerf.steps)
    sdf_vals = self.sdf(pts)
    # record mins along rays for backprop
    self.min_along_rays = sdf_vals.min(dim=0)[0]
    # values (useful for density), normals (useful for view), latent (useful for ???)
    sdf_latent = torch.cat([sdf_vals, self.sdf.normals, self.sdf.mlp.last_layer_out], dim=-1)
    self.nerf.set_per_pt_latent(sdf_latent)
    return self.nerf.from_pts(pts, ts, r_o, r_d)
  @property
  def density(self): return self.nerf.density
  def render(self, rays):
    r_o, r_d = rays.split([3,3], dim=-1)
    pts, hits, ts = self.sdf.sphere_march(r_o, r_d, near=self.nerf.t_near, far=self.nerf.t_far)
    # TODO convert vals to some RGB value
    vals = torch.ones_like(pts)
    return torch.where(hits, vals, torch.zeros_like(vals))
