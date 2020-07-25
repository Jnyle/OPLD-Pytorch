import torch
import torch.nn.functional as F
from torch import nn

from rcnn.utils.box_coder import BoxCoder
from rcnn.modeling.rpn.loss import make_rpn_loss_evaluator
from rcnn.modeling.rpn.anchor_generator import make_anchor_generator
from rcnn.modeling.rpn.inference import make_rpn_postprocessor
from rcnn.core.config import cfg


class RPNHead(nn.Module):
    """
    Adds a simple RPN Head with classification and regression heads
    """

    def __init__(self, dim_in, num_anchors):
        """
        Arguments:
            dim_in (int): number of channels of the input feature
            num_anchors (int): number of anchors to be predicted
        """
        super(RPNHead, self).__init__()
        self.dim_in = dim_in[-1]

        self.conv = nn.Conv2d(self.dim_in, self.dim_in, kernel_size=3, stride=1, padding=1)
        self.cls_logits = nn.Conv2d(self.dim_in, num_anchors, kernel_size=1, stride=1)
        self.bbox_pred = nn.Conv2d(self.dim_in, num_anchors * 4, kernel_size=1, stride=1)

        for l in [self.conv, self.cls_logits, self.bbox_pred]:
            nn.init.normal_(l.weight, std=0.01)
            nn.init.constant_(l.bias, 0)

    def forward(self, x):
        logits = []
        bbox_reg = []
        for feature in x:
            t = F.relu(self.conv(feature))
            logits.append(self.cls_logits(t))
            bbox_reg.append(self.bbox_pred(t))
        return logits, bbox_reg


class RPNModule(torch.nn.Module):
    """
    Module for RPN computation. Takes feature maps from the backbone and RPN
    proposals and losses. Works for both FPN and non-FPN.
    """

    def __init__(self, dim_in):
        super(RPNModule, self).__init__()

        anchor_generator = make_anchor_generator(cfg)
        head = RPNHead(dim_in, anchor_generator.num_anchors_per_location()[0])
        rpn_box_coder = BoxCoder(weights=(1.0, 1.0, 1.0, 1.0))
        box_selector_train = make_rpn_postprocessor(rpn_box_coder, is_train=True)
        box_selector_test = make_rpn_postprocessor(rpn_box_coder, is_train=False)
        loss_evaluator = make_rpn_loss_evaluator(rpn_box_coder)

        self.anchor_generator = anchor_generator
        self.head = head
        self.box_selector_train = box_selector_train
        self.box_selector_test = box_selector_test
        self.loss_evaluator = loss_evaluator

    def forward(self, images, features, targets=None):
        """
        Arguments:
            images (ImageList): images for which we want to compute the predictions
            features (list[Tensor]): features computed from the images that are
                used for computing the predictions. Each tensor in the list
                correspond to different feature levels
            targets (list[BoxList): ground-truth boxes present in the image (optional)

        Returns:
            boxes (list[BoxList]): the predicted boxes from the RPN, one BoxList per
                image.
            losses (dict[Tensor]): the losses for the model during training. During
                testing, it is an empty dict.
        """
        objectness, rpn_box_regression = self.head(features)
        anchors = self.anchor_generator(images, features)

        if self.training:
            return self._forward_train(anchors, objectness, rpn_box_regression, targets)
        else:
            return self._forward_test(anchors, objectness, rpn_box_regression)

    def _forward_train(self, anchors, objectness, rpn_box_regression, targets):
        if cfg.RPN.RPN_ONLY:
            # When training an RPN-only model, the loss is determined by the
            # predicted objectness and rpn_box_regression values and there is
            # no need to transform the anchors into predicted boxes; this is an
            # optimization that avoids the unnecessary transformation.
            boxes = anchors
        else:
            # For end-to-end models, anchors must be transformed into boxes and
            # sampled into a training batch.
            with torch.no_grad():
                boxes = self.box_selector_train(anchors, objectness, rpn_box_regression, targets)
        loss_rpn_objectness, loss_rpn_box_reg = self.loss_evaluator(
            anchors, objectness, rpn_box_regression, targets
        )
        losses = dict(loss_rpn_objectness=loss_rpn_objectness, loss_rpn_box_reg=loss_rpn_box_reg)
        return boxes, losses

    def _forward_test(self, anchors, objectness, rpn_box_regression):
        boxes = self.box_selector_test(anchors, objectness, rpn_box_regression)
        if cfg.RPN.RPN_ONLY:
            # For end-to-end models, the RPN proposals are an intermediate state
            # and don't bother to sort them in decreasing score order. For RPN-only
            # models, the proposals are the final output and we return them in
            # high-to-low confidence order.
            inds = [box.get_field("objectness").sort(descending=True)[1] for box in boxes]
            boxes = [box[ind] for box, ind in zip(boxes, inds)]
        return boxes, {}


def build_rpn(dim_in):
    return RPNModule(dim_in)