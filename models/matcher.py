# ------------------------------------------------------------------------
# Modified from DETR (https://github.com/facebookresearch/detr)
# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved.
# ------------------------------------------------------------------------
# Modified from Deformable DETR (https://github.com/fundamentalvision/Deformable-DETR)
# Copyright (c) 2020 SenseTime. All Rights Reserved.
# ------------------------------------------------------------------------

import torch
from scipy.optimize import linear_sum_assignment
from torch import nn

from util.box_ops import box_cxcywh_to_xyxy, generalized_box_iou
from util.losses import matched_l1_loss
from util.oriented_iou_loss import cal_giou

class HungarianMatcher(nn.Module):
    """This class computes an assignment between the targets and the predictions of the network
    For efficiency reasons, the targets don't include the no_object. Because of this, in general,
    there are more predictions than targets. In this case, we do a 1-to-1 matching of the best predictions,
    while the others are un-matched (and thus treated as non-objects).
    """

    def __init__(self, cost_class: float = 1, cost_bbox: float = 1, cost_giou: float = 1):
        """Creates the matcher
        Params:
            cost_class: This is the relative weight of the classification error in the matching cost
            cost_bbox: This is the relative weight of the L1 error of the bounding box coordinates in the matching cost
            cost_giou: This is the relative weight of the giou loss of the bounding box in the matching cost
        """
        super().__init__()
        self.cost_class = cost_class
        self.cost_bbox = cost_bbox
        self.cost_giou = cost_giou
        assert cost_class != 0 or cost_bbox != 0 or cost_giou != 0, "all costs cant be 0"

    @torch.no_grad()
    def forward(self, outputs, targets):
        """ Performs the matching
        Params:
            outputs: This is a dict that contains at least these entries:
                 "pred_logits": Tensor of dim [batch_size, num_queries, num_classes] with the classification logits
                 "pred_boxes": Tensor of dim [batch_size, num_queries, 8] with the predicted box coordinates
                 "pred_labels": Tensor of dim [batch_size, num_queries, num_angle_classes] with the angle classification logits
            targets: This is a list of targets (len(targets) = batch_size), where each target is a dict containing:
                 "labels": Tensor of dim [num_target_boxes] (where num_target_boxes is the number of ground-truth
                           objects in the target) containing the class labels
                 "boxes": Tensor of dim [num_target_boxes, 8] containing the target box coordinates
        Returns:
            A list of size batch_size, containing lists of [index_i, index_j, i_j_iou] where:
                - index_i is the indices of the selected predictions (in order)
                - index_j is the indices of the corresponding selected targets (in order)
                - i_j_iou is the iou of ith box between jth box
            For each batch element, it holds:
                len(index_i) = len(index_j) = len(i_j_iou) = min(num_queries, num_target_boxes)
        """
        bs, num_queries = outputs["pred_logits"].shape[:2]

        # We flatten to compute the cost matrices in a batch
        out_prob = outputs["pred_logits"].flatten(0, 1).sigmoid()  # [batch_size * num_queries, num_classes]
        out_oboxes = outputs["pred_boxes"].flatten(0, 1)  # [batch_size * num_queries, 5]
        out_polys = outputs["pred_polys"].flatten(0, 1)  # [batch_size, num_queries, 8]

        # Also concat the target labels and boxes
        tgt_ids = torch.cat([v["labels"] for v in targets])
        tgt_polys = torch.cat([v["polys"] for v in targets])
        tgt_boxes = torch.cat([v["boxes"] for v in targets])
        tgt_theta = torch.cat([v["theta"] for v in targets])
        tgt_oboxes = torch.cat([tgt_boxes, tgt_theta], dim=-1)

        # Compute the classification cost.
        alpha = 0.25
        gamma = 2.0
        neg_cost_class = (1 - alpha) * (out_prob ** gamma) * (-(1 - out_prob + 1e-8).log())
        pos_cost_class = alpha * ((1 - out_prob) ** gamma) * (-(out_prob + 1e-8).log())
        cost_class = pos_cost_class[:, tgt_ids] - neg_cost_class[:, tgt_ids]
        # print('cost_class', cost_class.shape, cost_class)

        num_gts = tgt_ids.shape[0]
        # Compute the L1 cost between boxes
        out_polys_tmp = out_polys.unsqueeze(1).repeat(1, num_gts, 1)
        tgt_polys_tmp = tgt_polys.unsqueeze(0).repeat(num_queries*bs, 1, 1)
        cost_bbox = matched_l1_loss(out_polys_tmp, tgt_polys_tmp)[0]
        # print('cost_bbox', cost_bbox.shape, cost_bbox)

        # Compute the iou cost betwen boxes
        out_oboxes_tmp = out_oboxes.unsqueeze(1).repeat(1, num_gts, 1)
        tgt_oboxes_tmp = tgt_oboxes.unsqueeze(0).repeat(num_queries*bs, 1, 1)
        cost_giou = cal_giou(out_oboxes_tmp, tgt_oboxes_tmp)
        # print('cost_iou', cost_giou.shape, cost_giou)

        # Final cost matrix
        C = self.cost_bbox * cost_bbox + self.cost_class * cost_class + self.cost_giou * cost_giou
        C = C.view(bs, num_queries, -1).cpu()

        sizes = [len(v["boxes"]) for v in targets]
        indices = [linear_sum_assignment(c[i]) for i, c in enumerate(C.split(sizes, -1))]
        #print('indices', indices)
        return [(torch.as_tensor(i, dtype=torch.int64), torch.as_tensor(j, dtype=torch.int64)) for i, j in indices]


def build_matcher(args):
    return HungarianMatcher(cost_class=args.set_cost_class, cost_bbox=args.set_cost_bbox, cost_giou=args.set_cost_giou)
