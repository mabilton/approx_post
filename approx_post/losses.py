
import numpy as np
import jax.numpy as jnp

class Loss:

    @staticmethod
    def _compute_loss_del_params(loss_del_phi, x, approxdist):

        if not hasattr(approxdist, 'phi_del_params'):
            loss_del_params = loss_del_phi
        else:
            phi_del_params = approxdist.phi_del_params(x)
            # Subtract 1 due to initial batch dimension:
            phi_ndims = np.ndims(loss_del_phi) - 1
            # Add singleton dimension for broadcasting puposes:
            phi_del_w = phi_del_w[None,:] # shape = (1, *phi_shapes, *w_shapes)
            
            # Perform multipilcation on transposes for broadcasting purposes - allows us to broadcast over w dimensions:
            loss_del_phi = loss_del_phi.T # shape = (*reverse(phi_shapes), num_batch)
            phi_del_w = phi_del_w.T # shape = (*reverse(w_shapes), *reverse(phi_shapes), 1)
            loss_del_params = phi_del_w * loss_del_phi # shape = (*reverse(w_shapes), *reverse(phi_shapes), num_batch)
            loss_del_params = loss_del_params.T # shape = (num_batches, *phi_shapes, *w_shapes)
            
            # Sum over phi values stored in different arraytainer elements:
            loss_del_params = loss_del_params.sum() # shape = (num_batches, *phi_shapes, *w_shapes)
            # Sum over phi values stored in same arraytainer elements:
            loss_del_params = loss_del_params.apply(lambda x, ndims: np.sum(x, axis=range(1,ndims+1)), args=(phi_ndims,)) # shape = (num_batches, *w_shapes)

        return loss_del_params

    def _apply_controlvariates(self, val, cv, num_batch, num_samples):

        val_vec = self._vectorise_controlvariate_input(val, new_shape=(num_batch, num_samples, -1))
        cv_vec = self._vectorise_controlvariate_input(cv, new_shape=(num_batch, num_samples, -1))
        
        var = self._compute_covariance(val_1=cv_vec, val_2=cv_vec) # shape = (num_batches, num_samples, dim_cv, dim_cv)
        cov = self._compute_covariance(val_1=cv_vec, val_2=val_vec, subtract_mean_from_val_2=True) # shape = (num_batches, num_samples, dim_cv, dim_val)
        
        cv_samples = self._compute_controlvariate_samples(val_vec, cov, var)
        
        val = self._unvectorise_controlvariate_output(cv_samples, val)

        return val
    
    @staticmethod
    def _vectorise_controlvariate_input(val, new_shape):
        flattened = val.flatten(order='F')
        return flattened.reshape(vectorised_shape, order='F')

    @staticmethod
    def _compute_covariance(val_1, val_2, subtract_mean_from_val_2=False):
        if subtract_mean_from_val_2:
            val_2 = val_2-np.mean(val_2, axis=1, keepdims=True)
        return np.mean(np.einsum("abi,abj->abij", val_1, val_2), axis=1)
    
    @staticmethod
    def _compute_controlvariate_samples(val_vec, cov, var):
        a = np.linalg.solve(var, cov) # shape = (num_batch, num_samples, dim_cv, dim_val)
        return val_vec - np.einsum("aij,abi->abj", a, cv_vec) # shape = (num_batch, num_samples, num_val)

    @staticmethod
    def _unvectorise_controlvariate_output(cv_samples, val):
        pass

    def _compute_joint_del_phi_reparameterisation(self, x, theta, transform_del_phi):
        joint_del_1 = self.joint.logprob_del_1(theta, x)
        return np.einsum("abj,abj...->ab...", joint_del_1, transform_del_phi) 
    
    @staticmethod
    def _compute_approx_del_phi_reparameterisation(approx, phi, theta, transform_del_phi, approx_is_mixture=False):
        if approx_is_mixture:
            approx_del_1 = approx.logprob_del_1(theta, phi) # shape = (num_batch, num_samples, theta_dim)
            approx_del_2 = approx.logprob_del_2(theta, phi) # shape = (num_batch, num_samples, *phi.shape)
            approx_del_phi = np.einsum("abj,abj...->ab...", approx_del_1, transform_del_phi) + approx_del_2
        else:
            approx_del_1 = approx.logprob_del_1_components(theta, phi) # shape = (num_batch, num_samples, theta_dim)
            approx_del_2 = approx.logprob_del_2_components(theta, phi) # shape = (num_batch, num_samples, *phi.shape)
            approx_del_phi = np.einsum("mabj,mabj...->mab...", approx_del_1, transform_del_phi) + approx_del_2
        return approx_del_phi

class ReverseKL(Loss):

    _default_num_samples = {'elbo_cv':1000, 'elbo_reparam':100, 'selbo_reparam': 10, 'selbo_cv': 100}

    def __init__(self, jointdist, use_reparameterisation=False, method='elbo'):
        if method.lower() not in self._methods:
            raise ValueError(f'Invalid method value provided; valid values are: {", ".join(self._methods)}')
        self.joint = jointdist
        self.use_reparameterisation = use_reparameterisation
        self.method = method
    
    def eval(self, approx, x, num_samples=None):

        if self.method == 'elbo':
            if self.use_reparameterisation:
                num_samples = self._default_num_samples['elbo_reparam'] if num_samples is None else num_samples
                loss, loss_del_phi = self._eval_elbo_reparameterisation(approx, x, num_samples)
            else:
                num_samples = self._default_num_samples['elbo_cv'] if num_samples is None else num_samples
                loss, loss_del_phi = self._eval_elbo_cv(approx, x, num_samples)
        elif self.method == 'selbo':
            if self.use_reparameterisation:
                num_samples = self._default_num_samples['selbo_reparam'] if num_samples is None else num_samples
                loss, loss_del_phi = self._eval_selbo_reparameterisation(approx, x, num_samples)
            else:
                num_samples = self._default_num_samples['selbo_cv'] if num_samples is None else num_samples
                loss, loss_del_phi = self._eval_selbo_cv(approx, x, num_samples)
        else:
            raise ValueError("Invalid method attribute value: must be either 'elbo' or 'selbo'.")
        
        loss_del_params = self._eval_loss_del_params(loss_del_phi, x, approx)

        return loss, loss_del_params

    def _eval_elbo_reparameterisation(self, approx, phi, x, num_samples):

        epsilon = approx.sample_base(num_samples)
        theta = approx.transform(epsilon, phi)

        approx_lp = approx.logprob(theta, phi)
        joint_lp = self.joint.logprob(theta, x)
        loss_samples = joint_lp - approx_lp

        transform_del_phi = approx.transform_del_2(epsilon, phi)
        joint_del_phi = self._compute_joint_del_phi(x, theta, transform_del_phi)
        approx_del_phi = self._compute_approx_del_phi(approx, phi, theta, transform_del_phi)
        loss_del_phi_samples = np.einsum("abi,abi...->ab...", joint_del_theta, transform_del_phi) \
                             - np.einsum("abi,abi...->ab...", approx_del_phi, transform_del_phi)
        
        loss = -1*np.mean(loss_samples, axis=1)
        loss_del_phi = -1*np.mean(loss_del_phi_samples, axis=1)
    
        return loss, loss_del_phi
        
    def _eval_elbo_cv(self, approx, phi, x, num_samples):

        theta = approx.sample(num_samples, phi) # shape = (num_batch, num_samples, theta_dim)

        approx_lp = approx.logpdf(theta_samples, phi) # shape = (num_batch, num_samples)
        joint_lp = self.joint.logpdf(theta_samples, x) # shape = (num_batch, num_samples)
        approx_del_phi = approx.logpdf_del_2(theta_samples, phi) # shape = (num_batch, num_samples, *phi.shape)
        
        loss_samples = (joint_lp - approx_lp) # shape = (num_batch, num_samples)
        loss_del_phi_samples = np.einsum("ab,ab...->ab...", loss_samples, approx_del_phi) # shape = (num_batch, num_samples, *phi.shape)

        control_variate = approx_del_phi
        num_batch = loss_samples.shape[0]
        loss_samples = self._apply_controlvariates(loss_samples, control_variate, num_batch, num_samples)
        loss_del_phi_samples = self._apply_controlvariates(loss_del_phi_samples, control_variate, num_batch, num_samples)

        loss = -1*np.mean(loss_samples, axis=1) # shape = (num_batch,)
        loss_del_phi = -1*np.mean(loss_del_phi_samples, axis=1) # shape = (num_batch, *phi.shape)

        return loss, loss_del_phi

    def _eval_selbo_reparameterisation(approx, phi, x, num_samples):
        
        epsilon = approx.sample_base(num_samples)
        theta = approx.transform(epsilon, phi)
    
        approx_lp = approx.logprob(theta, phi)
        joint_lp = self.joint.logprob(theta, x)
        loss_samples = joint_lp - approx_lp

        transform_del_phi = approx.transform_del_2(epsilon, phi)
        joint_del_phi = self._compute_joint_del_phi_reparameterisation(x, theta, transform_del_phi)
        approx_del_phi = \
            self._compute_approx_del_phi_reparameterisation(approx, phi, theta, transform_del_phi, approx_is_mixture=True)
        # Notice the mixture component dimension 'm' here:
        loss_del_phi_samples = np.einsum("abi,mabi...->mab...", joint_del_theta, transform_del_phi) \
                             - np.einsum("mabi,mabi...->mab...", approx_del_phi, transform_del_phi)

        coefficients = approx.coefficients(phi=phi)
        loss = np.mean(np.einsum("m, mab->ab", coefficients, loss_samples), axis=1)
        loss_del_phi = np.einsum("m, mab->ab", coefficients, loss_del_phi_samples)

        return loss, loss_del_phi

    def _eval_selbo_cv():
        pass

class ForwardKL(Loss):

    def __init__(self,  jointdist=None, posterior_samples=None, use_reparameterisation=False):
        self.joint = jointdist
        self.posterior_samples = posterior_samples
        self.use_reparameterisation = use_reparameterisation

    @staticmethod
    def _compute_importance_samples(samples, approx_logprob, joint_logprob)
        log_wts = joint_logprob - approx_logprob
        log_wts_max = np.max(log_wts, axis=1).reshape(-1,1)
        unnorm_wts = np.exp(log_wts-log_wts_max)
        return unnorm_wts*samples/np.sum(unnorm_wts, axis=1).reshape(-1,1)

    def eval(self, approx, x, num_samples=1000):
        
        if all(not hasattr(self, attr) for attr in ['joint', 'posterior_samples']):
            raise ValueError('Must specify either jointdist or posterior_samples.')

        phi = approx.phi(x)

        if self.posterior_samples is not None:
            loss, loss_del_phi = self._eval_posterior_samples(approx, phi)
        else:
            if self.use_reparameterisation:
                loss, loss_del_phi = self._eval_reparameterisation(approx, phi, x, num_samples)
            else:
                loss, loss_del_phi = self._eval_controlvariates(approx, phi, x, num_samples)
            
        loss_del_params = self._eval_loss_del_params(loss_del_phi, x, approx)

        return loss, loss_del_params

    def _eval_posterior_samples(self, approxdist, phi, x, num_samples):
        approx_lp = approx.logprob(self.posterior_samples, phi=phi)
        loss = -1*jnp.mean(approx_lp, axis=1)
        approx_del_phi = approx.logprob_del_2(self.posterior_samples, phi=phi)
        grad = -1*np.mean(approx_del_phi, axis=1)
        return loss, grad

    def _eval_reparameterisation(self, approx, phi, x, num_samples):
        
        epsilon = approx.sample_base(num_samples)
        theta = approx.transform(epsilon, phi)

        approx_lp = approx.logprob(theta, phi) # shape = (num_batch, num_samples)
        loss_samples = approx_lp
        
        transform_del_phi = approx.transform_del_2(epsilon, phi)
        joint_del_phi = self._compute_joint_del_phi_reparameterisation(x, theta, transform_del_phi)
        approx_del_phi = self._compute_approx_del_phi_reparameterisation(approx, phi, theta, transform_del_phi)
        loss_del_phi_samples = np.einsum("ab,ab...->ab...", approx_lp, joint_del_phi) + \
                               np.einsum("ab,ab...->ab...", 1-approx_lp, approx_del_phi)

        joint_lp = self.joint.logprob(theta, x) # shape = (num_batch, num_samples)
        loss_samples = self._compute_importance_samples(loss_samples, approx_lp, joint_lp)
        loss_del_phi_samples = self._compute_importance_samples(loss_del_phi_samples, approx_lp, joint_lp)
        
        loss = -1*np.mean(loss_samples, axis=1) # shape = (num_batch,)
        loss_del_phi = -1*np.mean(loss_del_phi_samples, axis=1) # shape = (num_batch, *phi.shape)

        return loss, loss_del_phi

    def _eval_controlvariates(self, approx, phi, x, num_samples, return_grad):
        
        theta = approx.sample(num_samples, phi) # shape = (num_batch, num_samples, theta_dim)

        approx_lp = approx.logprob(theta_samples, phi)  # shape = (num_batch, num_samples)
        approx_del_phi_samples = approx.lp_del_2(theta_samples, phi)

        loss_samples = approx_lp
        joint_lp = joint.logprob(theta_samples, x)
        loss_samples = self._compute_importance_samples(loss_samples, approx_lp, joint_lp)
        loss_del_phi_samples = self._compute_importance_samples(loss_del_phi_samples, approx_lp, joint_lp)

        # Apply control variates:
        control_variate = approx_del_phi_samples
        num_batch = loss_samples.shape[0]
        loss_samples = self._apply_controlvariates(loss_samples, control_variate, num_batch, num_samples)
        loss_del_phi_samples = self._apply_controlvariates(loss_del_phi_samples, control_variate, num_batch, num_samples)
        
        loss = -1*np.mean(loss_samples, axis=1) # shape = (num_batch,)
        loss_del_phi = -1*np.mean(loss_del_phi_samples, axis=1) # shape = (num_batch, *phi.shape)

        return loss, loss_del_phi