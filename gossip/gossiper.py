import torch
import torch.distributed as dist
import copy

from .graph_manager import GraphManager
from .mixing_manager import MixingManager
from .mixing_manager import UniformMixing
from .utils import (unsparsify_layerwise, flatten_tensors)



class dist_backend:
    UNDEFINED = -1
    TCP = 0
    MPI = 1
    GLOO = 2
    NCCL = 3


class Gossiper(object):
    """ Generic gossip averaging object for multi-peer communication """

    def __init__(self, msg, graph, device=None, mixing=None, logger=None,
                 rank=None, world_size=None):
        """
        Initialize generic averaging class designed for multi-peer comms

        :param msg: (tensor) message used to initialize recv buffer
        :param device: (device) device on which to initialize recv buffer
        :param graph: (GraphManager) Subclass of GraphManager
        :param mixing: (MixingManager) Subclass of MixingManager
        :param logger: (python logger) module used to log results
        """

        self.logger = logger
        if rank is None or world_size is None:
            assert dist.is_initialized()
            # for now p2p communication only supported withed tcp and mpi
            assert dist._backend != dist_backend.GLOO
            assert dist._backend != dist_backend.NCCL
            rank = dist.get_rank()
            world_size = dist.get_world_size()

        # graph topology properties
        self.rank = rank
        self.world_size = world_size
        assert isinstance(graph, GraphManager)
        self._graph_manager = graph
        self.peers_per_itr_device = torch.tensor(
            [self._graph_manager.peers_per_itr], device=device,
            dtype=msg.dtype)
        self.passive = self._graph_manager.is_passive()
        self.refresh_peers_(iteration=0, epoch=0, rotate=False)  # sets in- and out-peers attributes

        # mixing matrix
        if mixing is None:
            mixing = UniformMixing(self._graph_manager, device)
        assert isinstance(mixing, MixingManager)
        self._mixing_manager = mixing
        self.refresh_mixing_weights_()  # sets mixing-weights attribute


        # msg buffers used during send/recv
        self.device = device if device is not None else msg.device
        self.out_msg_buffer = []
        self.in_msg_buffer = msg.clone().detach_().to(self.device)
        
        if self.device.type == 'cpu':
            try:
                self.in_msg_buffer = self.in_msg_buffer.pin_memory()
            except Exception as e:
                if self.logger is not None:
                    self.logger.error(e)
        self.placeholder = self.in_msg_buffer.clone()
        #print(self.placeholder.size())

        self._pending_req = None

    @property
    def peers_per_itr(self):
        return self._graph_manager.peers_per_itr

    @peers_per_itr.setter
    def peers_per_itr(self, v):
        self._graph_manager.peers_per_itr = v

    def refresh_peers_(self, iteration, epoch, rotate=None):
        """ Update in- and out-peers """
        if self._graph_manager.is_dynamic_graph():
            if iteration%1==0:
                rotate = True
        self.out_edges, self.in_edges = self._graph_manager.get_edges(rotate)

    def refresh_mixing_weights_(self, residual_adjusted=False):
        """ Update mixing-matrix weights """
        self.mixing_weights = self._mixing_manager.get_mixing_weights(
            residual_adjusted)
        

    def mix_out_msg_(self, out_msg, ps_weight, residual=False):
        """ Returns a generator mixing messages on the fly """
        self.refresh_mixing_weights_(residual)
        self.ps_weight = ps_weight

        # check whether or not we need to communicate ps_weight
        # if not self.regular:
        out_msg = torch.cat([out_msg, self.ps_weight.type(out_msg.dtype)])

        # first return 'loopback msg to self'
        if not residual:
            yield out_msg.mul(self.mixing_weights['uniform'].type(out_msg.dtype))

        # check whether or not we need to create a buffer for each out-msg
        if self._mixing_manager.is_uniform():
            weight = self.mixing_weights['uniform']
            out_msg *= weight.type(out_msg.dtype)
            for _ in self.out_edges:
                yield out_msg
        else:
            for out_edge in self.out_edges:
                weight = self.mixing_weights[out_edge.dest]
                yield out_msg.mul(weight.type(out_msg.dtype))
                
    def mix_self_msg_(self, out_msg, ps_weight, residual=False):
        """ Returns a generator mixing messages on the fly """
        self.refresh_mixing_weights_(residual)
        self.ps_weight = ps_weight

        out_msg = torch.cat([out_msg, self.ps_weight.type(out_msg.dtype)])
       
        if self._mixing_manager.is_uniform():
            weight = self.mixing_weights['uniform']-1.0
            out_msg *= weight.type(out_msg.dtype)
            yield out_msg

    def clean_msg_buffers_(self):
        """ Clean outgoing message buffer """
        msgs = []
        while len(self.out_msg_buffer) > 0:
            req, msg = self.out_msg_buffer.pop()
            req.wait()
            msgs.append(msg)
        while len(msgs) > 0:
            msg = msgs.pop()
            with torch.no_grad():
                msg.set_()

    def parse_in_msg_buffer(self, residual=False):
        """ Parse in-msg buffer and return msg and ps-weight separately """
        msg = self.in_msg_buffer
        if not self.regular:
            return msg.narrow(0, 0, len(msg) - 1), msg[-1]
        else:
            if residual:
                return msg, self.ps_weight * self.peers_per_itr_device
            else:
                return msg, torch.ones(1, device=self.device).type(msg.dtype)

    def mix(self):
        """ Single gossip step """
        raise NotImplementedError



class SGD_DS(Gossiper):
    """ 1-peer Push-Sum consensus averaging module """

    def mix(self, out_msg, ref_msg, ps_weight, epoch, iteration, residual=False):
        """ Consensus averaging step """
        # out_msg must be on the correct device
        assert out_msg.device.type == self.device.type
        if self.logger is not None:
            self.logger.debug('in/out -peers {}/{}'
                              .format(self.in_edges, self.out_edges))

        # prepare messages for gossip
        out_copy    = copy.deepcopy(out_msg)
        ps_copy     = copy.deepcopy(ps_weight)
        placeholder = torch.zeros_like(torch.cat([out_msg,ps_weight]))
        mixed_out_msgs = self.mix_out_msg_(out_msg, ps_weight, residual)
       
        # non-blocking send
        data_amt = 0
        for out_edge in self.out_edges:
            msg = next(mixed_out_msgs)
           
            assert self.rank == out_edge.src 
            #print('rank, out edge:', self.rank, out_edge.dest)
            req = dist.broadcast(tensor=msg, src=out_edge.src,
                                 group=out_edge.process_group, async_op=True)
            self.out_msg_buffer.append((req, msg))
          
            data_amt += msg.element_size()*msg.nelement()
        # blocking recv w/ some code optimization to avoid buffer prep overhead
        
        self.in_msg_buffer.zero_()
        mixed_self_msg = self.mix_self_msg_(out_copy, ps_copy, residual)
                
        msg_mix = next(mixed_self_msg)
        self.in_msg_buffer.copy_(msg_mix)
            
        for in_edge in self.in_edges:
            dist.broadcast(tensor=placeholder, src=in_edge.src,
                               group=in_edge.process_group)
            self.placeholder.copy_(placeholder) 
            self.in_msg_buffer.add_(self.placeholder)
                

        self.refresh_peers_(epoch=epoch, iteration=iteration)
        self.clean_msg_buffers_()
        in_msg = self.in_msg_buffer.narrow(0, 0, len(self.in_msg_buffer) - 1)
        updated_ps_weight = self.in_msg_buffer[-1]
        return in_msg, updated_ps_weight, data_amt
