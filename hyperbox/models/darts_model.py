from typing import Any, List, Optional, Union

import copy
import random
import hydra
import numpy as np
import torch
import torch.nn as nn
from omegaconf import DictConfig

from hyperbox.utils.logger import get_logger
logger = get_logger(__name__, rank_zero=True)

from .base_model import BaseModel

class DARTSModel(BaseModel):
    def __init__(
        self,
        network_cfg: Optional[Union[DictConfig, dict]] = None,
        mutator_cfg: Optional[Union[DictConfig, dict]] = None,
        optimizer_cfg: Optional[Union[DictConfig, dict]] = None,
        loss_cfg: Optional[Union[DictConfig, dict]] = None,
        metric_cfg: Optional[Union[DictConfig, dict]] = None,
        arc_lr: float = 0.001,
        unrolled: bool = False,
        aux_weight: float = 0.4,
        is_net_parallel: bool = False,
        **kwargs
    ):
        super().__init__(network_cfg, mutator_cfg, optimizer_cfg,
                         loss_cfg, metric_cfg, **kwargs)
        self.arc_lr = arc_lr
        self.unrolled = unrolled
        self.aux_weight = aux_weight
        self.automatic_optimization = False
        self.is_net_parallel = is_net_parallel

    def sample_search(self):
        with torch.no_grad():
            if self.trainer.world_size <= 1:
                self.mutator.reset()
            else:
                logger.debug(f"process {self.rank} model is on device: {self.device}")
                mask_dict = dict()
                is_reset = False
                for idx in range(self.trainer.world_size):
                    if self.is_net_parallel or not is_reset:
                        self.mutator.reset()
                        is_reset = True
                    mask = self.mutator._cache
                    mask_dict[idx] = mask
                mask_dict = self.trainer.accelerator.broadcast(mask_dict, src=0)
                mask = mask_dict[self.rank]
                # logger.debug(f"[net_parallel={self.is_net_parallel}]{self.rank}: {mask['ValueChoice1']}")
                for m in self.mutator.mutables:
                    m.mask.data = mask[m.key].data.to(self.device)
                logger.info(f"[rank {self.rank}] arch={self.network.arch}")

    def training_step(self, batch: Any, batch_idx: int, optimizer_idx: int):
        (trn_X, trn_y) = batch['train']
        (val_X, val_y) = batch['val']
        self.weight_optim, self.ctrl_optim = self.optimizers()

        # phase 1. architecture step
        self.ctrl_optim.zero_grad()
        if self.unrolled:
            self._unrolled_backward(trn_X, trn_y, val_X, val_y)
        else:
            self._backward(val_X, val_y)
        self.ctrl_optim.step()

        # phase 2: child network step
        self.weight_optim.zero_grad()
        self.sample_search()
        preds, loss = self._logits_and_loss(trn_X, trn_y)
        # self.manual_backward(loss)
        loss.backward()
        nn.utils.clip_grad_norm_(self.network.parameters(), 5.)  # gradient clipping
        self.weight_optim.step()

        # log train metrics
        preds = torch.argmax(preds, dim=1)
        acc = self.train_metric(preds, trn_y)
        self.log("train/loss", loss, on_step=True, on_epoch=True, prog_bar=False)
        self.log("train/acc", acc, on_step=True, on_epoch=True, prog_bar=False)
        if batch_idx % 50 ==0:
            logger.info(f"Train epoch{self.current_epoch} batch{batch_idx}: loss={loss}, acc={acc}")
        return {"loss": loss, "preds": preds, "targets": trn_y, 'acc': acc}

    def _logits_and_loss(self, X, y):
        output = self.network(X)
        if isinstance(output, tuple):
            output, aux_output = output
            aux_loss = self.criterion(aux_output, y)
        else:
            aux_loss = 0.
        loss = self.criterion(output, y)
        loss = loss + self.aux_weight * aux_loss
        # self._write_graph_status()
        return output, loss

    def _backward(self, val_X, val_y):
        """
        Simple backward with gradient descent
        """
        self.sample_search()
        _, loss = self._logits_and_loss(val_X, val_y)
        loss.backward()
        # self.manual_backward(loss)

    def _unrolled_backward(self, trn_X, trn_y, val_X, val_y):
        """
        Compute unrolled loss and backward its gradients
        """
        backup_params = copy.deepcopy(tuple(self.network.parameters()))

        # do virtual step on training data
        lr = self.optimizer.param_groups[0]["lr"]
        momentum = self.optimizer.param_groups[0]["momentum"]
        weight_decay = self.optimizer.param_groups[0]["weight_decay"]
        self._compute_virtual_model(trn_X, trn_y, lr, momentum, weight_decay)

        # calculate unrolled loss on validation data
        # keep gradients for model here for compute hessian
        self.sample_search()
        _, loss = self._logits_and_loss(val_X, val_y)
        w_model, w_ctrl = tuple(self.network.parameters()), tuple(self.mutator.parameters())
        w_grads = torch.autograd.grad(loss, w_model + w_ctrl)
        d_model, d_ctrl = w_grads[:len(w_model)], w_grads[len(w_model):]

        # compute hessian and final gradients
        hessian = self._compute_hessian(backup_params, d_model, trn_X, trn_y)
        with torch.no_grad():
            for param, d, h in zip(w_ctrl, d_ctrl, hessian):
                # gradient = dalpha - lr * hessian
                param.grad = d - lr * h

        # restore weights
        self._restore_weights(backup_params)

    def _compute_virtual_model(self, X, y, lr, momentum, weight_decay):
        """
        Compute unrolled weights w`
        """
        # don't need zero_grad, using autograd to calculate gradients
        self.sample_search()
        _, loss = self._logits_and_loss(X, y)
        gradients = torch.autograd.grad(loss, self.network.parameters())
        with torch.no_grad():
            for w, g in zip(self.network.parameters(), gradients):
                m = self.optimizer.state[w].get("momentum_buffer", 0.)
                w = w - lr * (momentum * m + g + weight_decay * w)

    def _restore_weights(self, backup_params):
        with torch.no_grad():
            for param, backup in zip(self.network.parameters(), backup_params):
                param.copy_(backup)

    def _compute_hessian(self, backup_params, dw, trn_X, trn_y):
        """
            dw = dw` { L_val(w`, alpha) }
            w+ = w + eps * dw
            w- = w - eps * dw
            hessian = (dalpha { L_trn(w+, alpha) } - dalpha { L_trn(w-, alpha) }) / (2*eps)
            eps = 0.01 / ||dw||
        """
        self._restore_weights(backup_params)
        norm = torch.cat([w.view(-1) for w in dw]).norm()
        eps = 0.01 / norm
        if norm < 1E-8:
            self.logger.warning("In computing hessian, norm is smaller than 1E-8, cause eps to be %.6f.", norm.item())

        dalphas = []
        for e in [eps, -2. * eps]:
            # w+ = w + eps*dw`, w- = w - eps*dw`
            with torch.no_grad():
                for p, d in zip(self.network.parameters(), dw):
                    p += e * d

            self.sample_search()
            _, loss = self._logits_and_loss(trn_X, trn_y)
            dalphas.append(torch.autograd.grad(loss, self.mutator.parameters()))

        dalpha_pos, dalpha_neg = dalphas  # dalpha { L_trn(w+) }, # dalpha { L_trn(w-) }
        hessian = [(p - n) / 2. * eps for p, n in zip(dalpha_pos, dalpha_neg)]
        return hessian

    def training_epoch_end(self, outputs: List[Any]):        
        acc = np.mean([output['acc'].item() for output in outputs])
        loss = np.mean([output['loss'].item() for output in outputs])
        mflops, size = self.arch_size((1,3,32,32), convert=True)
        logger.info(f"[rank {self.rank}] current model({self.arch}): {mflops:.4f} MFLOPs, {size:.4f} MB.")
        logger.info(f"[rank {self.rank}] Train epoch{self.current_epoch} final result: loss={loss}, acc={acc}")

    def validation_step(self, batch: Any, batch_idx: int):
        (X, targets) = batch
        preds, loss = self._logits_and_loss(X, targets)
        preds = torch.argmax(preds, dim=1)

        # log val metrics
        acc = self.val_metric(preds, targets)
        self.log("val/loss", loss, on_step=False, on_epoch=True, prog_bar=False)
        self.log("val/acc", acc, on_step=False, on_epoch=True, prog_bar=False)
        # if batch_idx % 10 == 0:
        # if True:
            # logger.info(f"Val epoch{self.current_epoch} batch{batch_idx}: loss={loss}, acc={acc}")
        return {"loss": loss, "preds": preds, "targets": targets, 'acc': acc}

    def validation_epoch_end(self, outputs: List[Any]):
        acc = np.mean([output['acc'].item() for output in outputs])
        loss = np.mean([output['loss'].item() for output in outputs])
        mflops, size = self.arch_size((1,3,32,32), convert=True)
        logger.info(f"[rank {self.rank}] current model({self.arch}): {mflops:.4f} MFLOPs, {size:.4f} MB.")
        logger.info(f"[rank {self.rank}] Val epoch{self.current_epoch} final result: loss={loss}, acc={acc}")
    
    def test_step(self, batch: Any, batch_idx: int):
        (X, targets) = batch
        preds, loss = self._logits_and_loss(X, targets)
        preds = torch.argmax(preds, dim=1)

        # log test metrics
        acc = self.test_metric(preds, targets)
        self.log("test/loss", loss, on_step=False, on_epoch=True)
        self.log("test/acc", acc, on_step=False, on_epoch=True)
        if batch_idx % 10 == 0:
            logger.info(f"Test batch{batch_idx}: loss={loss}, acc={acc}")
        return {"loss": loss, "preds": preds, "targets": targets, 'acc': acc}

    def test_epoch_end(self, outputs: List[Any]):
        acc = np.mean([output['acc'].item() for output in outputs])
        loss = np.mean([output['loss'].item() for output in outputs])
        logger.info(f"[rank {self.rank}] Test epoch{self.current_epoch} final result: loss={loss}, acc={acc}")


    def configure_optimizers(self):
        """Choose what optimizers and learning-rate schedulers to use in your optimization.
        Normally you'd need one. But in the case of GANs or similar you might have multiple.

        See examples here:
            https://pytorch-lightning.readthedocs.io/en/latest/common/lightning_module.html#configure-optimizers
        """
        optimizer_cfg = DictConfig(self.hparams.optimizer_cfg)
        weight_optim = hydra.utils.instantiate(optimizer_cfg, params=self.network.parameters())
        ctrl_optim = torch.optim.Adam(
            self.mutator.parameters(), self.arc_lr, betas=(0.5, 0.999), weight_decay=1.0E-3)
        return weight_optim, ctrl_optim
